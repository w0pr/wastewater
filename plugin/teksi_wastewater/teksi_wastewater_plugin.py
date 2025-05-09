# -----------------------------------------------------------
#
# TEKSI Wastewater
#
# Copyright (C) 2012  Matthias Kuhn
# -----------------------------------------------------------
#
# licensed under the terms of GNU GPL 2
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this progsram; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# ---------------------------------------------------------------------


import logging
import os
import shutil

from qgis.core import Qgis, QgsApplication
from qgis.PyQt.QtCore import QLocale, QSettings, Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QApplication, QMessageBox, QToolBar
from qgis.utils import qgsfunction

try:
    from .gui.twwplotsvgwidget import TwwPlotSVGWidget
except ImportError:
    TwwPlotSVGWidget = None
from .gui.twwprofiledockwidget import TwwProfileDockWidget
from .gui.twwsettingsdialog import TwwSettingsDialog
from .gui.twwwizard import TwwWizard
from .libs.modelbaker.iliwrapper.ili2dbutils import JavaNotFoundError
from .processing_provider.provider import TwwProcessingProvider
from .tools.twwmaptools import TwwMapToolConnectNetworkElements, TwwTreeMapTool
from .tools.twwnetwork import TwwGraphManager
from .utils.database_utils import DatabaseUtils
from .utils.plugin_utils import plugin_root_path
from .utils.qt_utils import OverrideCursor
from .utils.translation import setup_i18n
from .utils.twwlayermanager import TwwLayerManager, TwwLayerNotifier
from .utils.twwlogging import TwwQgsLogHandler

LOGFORMAT = "%(asctime)s:%(levelname)s:%(module)s:%(message)s"


@qgsfunction(0, "System")
def locale(values, feature, parent):
    return QSettings().value("locale/userLocale", QLocale.system().name())


class TeksiWastewaterPlugin:
    """
    A plugin for wastewater management
    https://github.com/teksi/wastewater
    """

    # The networkAnalyzer will manage the networklayers and pathfinding
    network_analyzer = None

    # Remember not to reopen the dock if there's already one opened
    profile_dock = None

    # Wizard
    wizarddock = None

    # The layer ids the plugin will need
    edgeLayer = None
    nodeLayer = None
    specialStructureLayer = None
    networkElementLayer = None

    profile = None

    def __init__(self, iface):
        if os.environ.get("QGIS_DEBUGPY_HAS_LOADED") is None and QSettings().value(
            "/TWW/DeveloperMode", False, type=bool
        ):
            try:
                import debugpy

                debugpy.configure(python=shutil.which("python"))
                debugpy.listen(("localhost", 5678))
            except Exception as e:
                print(f"Unable to create debugpy debugger: {e}")
            else:
                os.environ["QGIS_DEBUGPY_HAS_LOADED"] = "1"

        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.nodes = None
        self.edges = None

        self.interlisImporterExporter = None

        self.initLogger()
        setup_i18n()

    def tr(self, source_text):
        """
        This does not inherit from QObject but for the translation to work (in particular to have translatable strings
        picked up) we need a tr method.
        :rtype : unicode
        :param source_text: The text to translate
        :return: The translated text
        """
        return QApplication.translate("TwwPlugin", source_text)

    def initLogger(self):
        """
        Initializes the logger
        """
        self.logger = logging.getLogger(__package__)

        settings = QSettings()

        loglevel = settings.value("/TWW/LogLevel", "Warning")
        logfile = settings.value("/TWW/LogFile", None)

        if hasattr(self.logger, "twwFileHandler"):
            self.logger.removeHandler(self.logger.twwFileHandler)
            del self.logger.twwFileHandler

        current_handlers = [h.__class__.__name__ for h in self.logger.handlers]
        if self.__class__.__name__ not in current_handlers:
            self.logger.addHandler(TwwQgsLogHandler())

        if logfile:
            log_handler = logging.FileHandler(logfile)
            fmt = logging.Formatter(LOGFORMAT)
            log_handler.setFormatter(fmt)
            self.logger.addHandler(log_handler)
            self.logger.fileHandler = log_handler

        if "Debug" == loglevel:
            self.logger.setLevel(logging.DEBUG)
        elif "Info" == loglevel:
            self.logger.setLevel(logging.INFO)
        elif "Warning" == loglevel:
            self.logger.setLevel(logging.WARNING)
        elif "Error" == loglevel:
            self.logger.setLevel(logging.ERROR)

        fp = os.path.join(os.path.abspath(os.path.dirname(__file__)), "metadata.txt")

        ini_text = QSettings(fp, QSettings.IniFormat)
        verno = ini_text.value("version")

        self.logger.info("TEKSI Wastewater plugin version " + verno + " started")

    def initGui(self):
        """
        Called to setup the plugin GUI
        """
        self.network_layer_notifier = TwwLayerNotifier(
            self.iface.mainWindow(),
            ["vw_network_node", "vw_network_segment"],
        )
        self.vw_tww_layer_notifier = TwwLayerNotifier(
            self.iface.mainWindow(),
            ["vw_tww_wastewater_structure"],
        )
        self.toolbarButtons = []

        # Create toolbar button
        # self.profileAction = QAction(
        #     QIcon(os.path.join(plugin_root_path(), "icons/wastewater-profile.svg")),
        #     self.tr("Profile"),
        #     self.iface.mainWindow(),
        # )
        # self.profileAction.setWhatsThis(self.tr("Reach trace"))
        # self.profileAction.setEnabled(False)
        # self.profileAction.setCheckable(True)
        # self.profileAction.triggered.connect(self.profileToolClicked)

        self.downstreamAction = QAction(
            QIcon(os.path.join(plugin_root_path(), "icons/wastewater-downstream.svg")),
            self.tr("Downstream"),
            self.iface.mainWindow(),
        )
        self.downstreamAction.setWhatsThis(self.tr("Downstream reaches"))
        self.downstreamAction.setEnabled(False)
        self.downstreamAction.setCheckable(True)
        self.downstreamAction.triggered.connect(self.downstreamToolClicked)

        self.upstreamAction = QAction(
            QIcon(os.path.join(plugin_root_path(), "icons/wastewater-upstream.svg")),
            self.tr("Upstream"),
            self.iface.mainWindow(),
        )
        self.upstreamAction.setWhatsThis(self.tr("Upstream reaches"))
        self.upstreamAction.setEnabled(False)
        self.upstreamAction.setCheckable(True)
        self.upstreamAction.triggered.connect(self.upstreamToolClicked)

        self.wizardAction = QAction(
            QIcon(os.path.join(plugin_root_path(), "icons/wizard.svg")),
            "Wizard",
            self.iface.mainWindow(),
        )
        self.wizardAction.setWhatsThis(self.tr("Create new manholes and reaches"))
        self.wizardAction.setEnabled(False)
        self.wizardAction.setCheckable(True)
        self.wizardAction.triggered.connect(self.wizard)

        self.connectNetworkElementsAction = QAction(
            QIcon(os.path.join(plugin_root_path(), "icons/link-wastewater-networkelement.svg")),
            QApplication.translate("teksi_wastewater", "Connect wastewater networkelements"),
            self.iface.mainWindow(),
        )
        self.connectNetworkElementsAction.setEnabled(False)
        self.connectNetworkElementsAction.setCheckable(True)
        self.connectNetworkElementsAction.triggered.connect(self.connectNetworkElements)

        self.refreshNetworkTopologyAction = QAction(
            QIcon(os.path.join(plugin_root_path(), "icons/refresh-network.svg")),
            "Refresh network topology",
            self.iface.mainWindow(),
        )
        self.refreshNetworkTopologyAction.setWhatsThis(self.tr("Refresh network topology"))
        self.refreshNetworkTopologyAction.setEnabled(False)
        self.refreshNetworkTopologyAction.setCheckable(False)
        self.refreshNetworkTopologyAction.triggered.connect(
            self.refreshNetworkTopologyActionClicked
        )

        self.updateSymbologyAction = QAction(self.tr("Update symbology"), self.iface.mainWindow())
        self.updateSymbologyAction.triggered.connect(self.updateSymbology)

        self.validityCheckAction = QAction(self.tr("Validity check"), self.iface.mainWindow())
        self.validityCheckAction.triggered.connect(self.tww_validity_check_action)

        self.enableSymbologyTriggersAction = QAction(
            self.tr("Enable symbology triggers"), self.iface.mainWindow()
        )
        self.enableSymbologyTriggersAction.triggered.connect(self.enable_symbology_triggers)

        self.disableSymbologyTriggersAction = QAction(
            self.tr("Disable symbology triggers"), self.iface.mainWindow()
        )
        self.disableSymbologyTriggersAction.triggered.connect(self.disable_symbology_triggers)

        self.settingsAction = QAction(
            QIcon(QgsApplication.getThemeIcon("/mActionOptions.svg")),
            self.tr("Settings"),
            self.iface.mainWindow(),
        )
        self.settingsAction.triggered.connect(self.showSettings)

        self.aboutAction = QAction(
            QIcon(os.path.join(plugin_root_path(), "icons/teksi-abwasser-logo.svg")),
            self.tr("About"),
            self.iface.mainWindow(),
        )
        self.aboutAction.triggered.connect(self.about)

        self.importAction = QAction(
            QIcon(os.path.join(plugin_root_path(), "icons/interlis_import.svg")),
            self.tr("Import from interlis"),
            self.iface.mainWindow(),
        )
        self.importAction.setWhatsThis(self.tr("Import from INTERLIS"))
        self.importAction.setEnabled(False)
        self.importAction.setCheckable(False)
        self.importAction.triggered.connect(self.actionImportClicked)

        self.exportAction = QAction(
            QIcon(os.path.join(plugin_root_path(), "icons/interlis_export.svg")),
            self.tr("Export to interlis"),
            self.iface.mainWindow(),
        )
        self.exportAction.setWhatsThis(self.tr("Export to INTERLIS"))
        self.exportAction.setEnabled(False)
        self.exportAction.setCheckable(False)
        self.exportAction.triggered.connect(self.actionExportClicked)

        # Add toolbar button and menu item
        self.toolbar = QToolBar(self.tr("TEKSI Wastewater"))
        self.toolbar.setObjectName(self.toolbar.windowTitle())
        # self.toolbar.addAction(self.profileAction)
        self.toolbar.addAction(self.upstreamAction)
        self.toolbar.addAction(self.downstreamAction)
        self.toolbar.addAction(self.wizardAction)
        self.toolbar.addAction(self.refreshNetworkTopologyAction)
        self.toolbar.addAction(self.connectNetworkElementsAction)

        self.main_menu_name = "TEKSI &Wastewater"
        # self.iface.addPluginToMenu(self.menu, self.profileAction)
        self.iface.addPluginToMenu(self.main_menu_name, self.updateSymbologyAction)
        self.iface.addPluginToMenu(self.main_menu_name, self.validityCheckAction)
        self.iface.addPluginToMenu(self.main_menu_name, self.enableSymbologyTriggersAction)
        self.iface.addPluginToMenu(self.main_menu_name, self.disableSymbologyTriggersAction)
        self.iface.addPluginToMenu(self.main_menu_name, self.settingsAction)
        self.iface.addPluginToMenu(self.main_menu_name, self.aboutAction)

        self._get_main_menu_action().setIcon(
            QIcon(os.path.join(plugin_root_path(), "icons/teksi-abwasser-logo.svg")),
        )

        self.update_admin_mode()

        self.iface.addToolBar(self.toolbar)

        # Local array of buttons to enable / disable based on context
        # self.toolbarButtons.append(self.profileAction)
        self.toolbarButtons.append(self.upstreamAction)
        self.toolbarButtons.append(self.downstreamAction)
        self.toolbarButtons.append(self.wizardAction)
        self.toolbarButtons.append(self.refreshNetworkTopologyAction)
        self.toolbarButtons.append(self.importAction)
        self.toolbarButtons.append(self.exportAction)

        self.network_layer_notifier.layersAvailable.connect(self.onNetworkLayersAvailable)
        self.network_layer_notifier.layersUnavailable.connect(self.onNetworkLayersUnavailable)

        self.vw_tww_layer_notifier.layersAvailable.connect(self.onTwwLayersAvailable)
        self.vw_tww_layer_notifier.layersUnavailable.connect(self.onTwwLayersUnavailable)

        # Init the object maintaining the network
        self.network_analyzer = TwwGraphManager()
        self.network_analyzer.message_emitted.connect(self.iface.messageBar().pushMessage)
        # Create the map tool for profile selection
        # self.profile_tool = TwwProfileMapTool(
        #    self.iface, self.profileAction, self.network_analyzer
        # )
        # self.profile_tool.profileChanged.connect(self.onProfileChanged)

        self.upstream_tree_tool = TwwTreeMapTool(
            self.iface, self.upstreamAction, self.network_analyzer
        )
        self.upstream_tree_tool.setDirection("upstream")
        self.upstream_tree_tool.treeChanged.connect(self.onTreeChanged)
        self.downstream_tree_tool = TwwTreeMapTool(
            self.iface, self.downstreamAction, self.network_analyzer
        )
        self.downstream_tree_tool.setDirection("downstream")
        self.downstream_tree_tool.treeChanged.connect(self.onTreeChanged)

        self.maptool_connect_networkelements = TwwMapToolConnectNetworkElements(
            self.iface, self.connectNetworkElementsAction
        )

        self.processing_provider = TwwProcessingProvider()
        QgsApplication.processingRegistry().addProvider(self.processing_provider)

        self.network_layer_notifier.layersAdded([])

    def tww_validity_check_startup(self):
        messages = []
        try:
            messages = DatabaseUtils.get_validity_check_issues()

        except Exception as exception:
            messages.append(self.tr(f"Could not check database validity: {exception}"))

        for message in messages:
            self.iface.messageBar().pushMessage(
                "Error",
                message,
                level=Qgis.Critical,
            )

    def tww_validity_check_action(self):
        messages = []
        try:
            messages = DatabaseUtils.get_validity_check_issues()

        except Exception as exception:
            messages.append(self.tr(f"Could not check database validity: {exception}"))

        if len(messages) == 0:
            QMessageBox.information(
                self.iface.mainWindow(),
                self.validityCheckAction.text(),
                self.tr("There are no database validity issues."),
            )
            return

        messagesText = "\n".join(messages)
        QMessageBox.critical(
            self.iface.mainWindow(),
            self.validityCheckAction.text(),
            self.tr(f"Database has following validity issues:\n\n{messagesText}"),
        )

    def enable_symbology_triggers(self):
        try:
            DatabaseUtils.enable_symbology_triggers()
            QMessageBox.information(
                self.iface.mainWindow(),
                self.enableSymbologyTriggersAction.text(),
                self.tr("Symbology triggers have been successfully enabled"),
            )

        except Exception as exception:
            QMessageBox.critical(
                self.iface.mainWindow(),
                self.enableSymbologyTriggersAction.text(),
                self.tr(f"Symbology triggers cannot be enabled:\n\n{exception}"),
            )

    def disable_symbology_triggers(self):
        try:
            DatabaseUtils.disable_symbology_triggers()
            QMessageBox.information(
                self.iface.mainWindow(),
                self.disableSymbologyTriggersAction.text(),
                self.tr("Symbology triggers have been successfully disabled"),
            )

        except Exception as exception:
            QMessageBox.critical(
                self.iface.mainWindow(),
                self.disableSymbologyTriggersAction.text(),
                self.tr(f"Symbology triggers cannot be disabled:\n\n{exception}"),
            )

    def unload(self):
        """
        Called when unloading
        """
        # self.toolbar.removeAction(self.profileAction)
        self.toolbar.removeAction(self.upstreamAction)
        self.toolbar.removeAction(self.downstreamAction)
        self.toolbar.removeAction(self.wizardAction)
        self.toolbar.removeAction(self.refreshNetworkTopologyAction)
        self.toolbar.removeAction(self.connectNetworkElementsAction)

        if self.importAction in self.toolbar.actions():
            self.toolbar.removeAction(self.importAction)
        if self.exportAction in self.toolbar.actions():
            self.toolbar.removeAction(self.exportAction)

        self.toolbar.deleteLater()

        # self.iface.removePluginMenu(self.menu, self.profileAction)
        self.iface.removePluginMenu(self.main_menu_name, self.updateSymbologyAction)
        self.iface.removePluginMenu(self.main_menu_name, self.validityCheckAction)
        self.iface.removePluginMenu(self.main_menu_name, self.settingsAction)
        self.iface.removePluginMenu(self.main_menu_name, self.aboutAction)
        self.iface.removePluginMenu(self.main_menu_name, self.enableSymbologyTriggersAction)
        self.iface.removePluginMenu(self.main_menu_name, self.disableSymbologyTriggersAction)

        QgsApplication.processingRegistry().removeProvider(self.processing_provider)

    def onNetworkLayersAvailable(self, layers):
        self.connectNetworkElementsAction.setEnabled(True)
        self.network_analyzer.setReachLayer(layers["vw_network_segment"])
        self.network_analyzer.setNodeLayer(layers["vw_network_node"])

    def onNetworkLayersUnavailable(self):
        self.connectNetworkElementsAction.setEnabled(False)

    def onTwwLayersAvailable(self):
        for b in self.toolbarButtons:
            b.setEnabled(True)

        self._configure_database_connection_config_from_tww_layer()
        self.tww_validity_check_startup()

    def onTwwLayersUnavailable(self):
        for b in self.toolbarButtons:
            b.setEnabled(False)

    def profileToolClicked(self):
        """
        Is executed when the profile button is clicked
        """
        self.openDock()
        # Set the profile map tool
        # self.profile_tool.setActive()

    def upstreamToolClicked(self):
        """
        Is executed when the user clicks the upstream search tool
        """
        self.openDock()
        self.upstream_tree_tool.setActive()

    def downstreamToolClicked(self):
        """
        Is executed when the user clicks the downstream search tool
        """
        self.openDock()
        self.downstream_tree_tool.setActive()

    def refreshNetworkTopologyActionClicked(self):
        """
        Is executed when the user clicks the refreshNetworkTopologyAction tool
        """
        self.network_analyzer.refresh()

    def wizard(self):
        """"""
        if not self.wizarddock:
            self.wizarddock = TwwWizard(self.iface.mainWindow(), self.iface)
        self.logger.debug("Opening Wizard")
        self.iface.addDockWidget(Qt.LeftDockWidgetArea, self.wizarddock)
        self.wizarddock.show()

    def connectNetworkElements(self, checked):
        self.iface.mapCanvas().setMapTool(self.maptool_connect_networkelements)

    def openDock(self):
        """
        Opens the dock
        """
        if self.profile_dock is None:
            self.logger.debug("Open dock")
            self.profile_dock = TwwProfileDockWidget(
                self.iface.mainWindow(),
                self.iface.mapCanvas(),
                self.iface.addDockWidget,
            )
            self.profile_dock.closed.connect(self.onDockClosed)
            self.profile_dock.showIt()

            self.plotWidget = None
            if TwwPlotSVGWidget is not None:
                self.plotWidget = TwwPlotSVGWidget(self.profile_dock, self.network_analyzer)
                self.plotWidget.specialStructureMouseOver.connect(self.highlightProfileElement)
                self.plotWidget.specialStructureMouseOut.connect(self.unhighlightProfileElement)
                self.plotWidget.reachMouseOver.connect(self.highlightProfileElement)
                self.plotWidget.reachMouseOut.connect(self.unhighlightProfileElement)
                self.profile_dock.addPlotWidget(self.plotWidget)
                self.profile_dock.setTree(self.nodes, self.edges)

    def onDockClosed(self):  # used when Dock dialog is closed
        """
        Gets called when the dock is closed
        All the cleanup of the dock has to be done here
        """
        self.profile_dock = None

    def onProfileChanged(self, profile):
        """
        The profile changed: update the plot
        @param profile: The profile to plot
        """
        self.profile = profile.copy()

        if self.plotWidget:
            self.plotWidget.setProfile(profile)

    def onTreeChanged(self, nodes, edges):
        if self.profile_dock:
            self.profile_dock.setTree(nodes, edges)
        self.nodes = nodes
        self.edges = edges

    def highlightProfileElement(self, obj_id):
        if self.profile is not None:
            self.profile.highlight(str(obj_id))

    def unhighlightProfileElement(self):
        if self.profile is not None:
            self.profile.highlight(None)

    def updateSymbology(self):
        try:
            with OverrideCursor(Qt.WaitCursor):
                DatabaseUtils.update_symbology()
            QMessageBox.information(
                self.iface.mainWindow(),
                self.updateSymbologyAction.text(),
                self.tr("Symbology has been successfully updated"),
            )

        except Exception as exception:
            QMessageBox.critical(
                self.iface.mainWindow(),
                self.updateSymbologyAction.text(),
                self.tr(f"Symbology update failed:\n\n{exception}"),
            )

    def showSettings(self):
        settings_dlg = TwwSettingsDialog(self.iface.mainWindow())
        settings_dlg.exec_()

        self.update_admin_mode()

    def about(self):
        from .gui.about_dialog import AboutDialog

        AboutDialog(self.iface.mainWindow()).exec_()

    def actionExportClicked(self):
        if self.interlisImporterExporter is None:
            try:
                # We only import now to avoid useless exception if dependencies aren't met
                from .interlis.gui.interlis_importer_exporter_gui import (
                    InterlisImporterExporterGui,
                )

                self.interlisImporterExporter = InterlisImporterExporterGui()

            except ImportError as e:
                self.iface.messageBar().pushMessage(
                    "Error",
                    "Could not load Interlis exporter due to unmet dependencies. See logs for more details.",
                    level=Qgis.Critical,
                )
                self.logger.error(str(e))
                return

            except JavaNotFoundError as e:
                self.iface.messageBar().pushMessage(
                    "Error",
                    "Could not load Interlis exporter due to missing Java. See logs for more details.",
                    level=Qgis.Critical,
                )
                self.logger.error(str(e))
                return

        try:
            self.interlisImporterExporter.check_dependencies()
        except Exception as exception:
            self.iface.messageBar().pushMessage(
                "Error",
                f"Could not load start the Interlis exporter due to unmet dependencies: {exception}.",
                level=Qgis.Critical,
            )
            self.logger.error(str(exception))
            return

        self.interlisImporterExporter.action_export()

    def actionImportClicked(self):
        if self.interlisImporterExporter is None:
            try:
                # We only import now to avoid useless exception if dependencies aren't met
                from .interlis.gui.interlis_importer_exporter_gui import (
                    InterlisImporterExporterGui,
                )

                self.interlisImporterExporter = InterlisImporterExporterGui()
            except ImportError as e:
                self.iface.messageBar().pushMessage(
                    "Error",
                    "Could not load Interlis importer due to unmet dependencies. See logs for more details.",
                    level=Qgis.Critical,
                )
                self.logger.error(str(e))
                return

            except JavaNotFoundError as e:
                self.iface.messageBar().pushMessage(
                    "Error",
                    "Could not load Interlis importer due to missing Java. See logs for more details.",
                    level=Qgis.Critical,
                )
                self.logger.error(str(e))
                return

        try:
            self.interlisImporterExporter.check_dependencies()
        except Exception as exception:
            self.iface.messageBar().pushMessage(
                "Error",
                f"Could not load start the Interlis importer due to unmet dependencies: {exception}.",
                level=Qgis.Critical,
            )
            self.logger.error(str(exception))
            return

        self.interlisImporterExporter.action_import()

    def _configure_database_connection_config_from_tww_layer(self) -> dict:
        """Configures tww2ili using the currently loaded TWW project layer"""

        pg_layer = TwwLayerManager.layer("vw_tww_wastewater_structure")
        if not pg_layer:
            self.iface.messageBar().pushMessage(
                "Error",
                "Could not determine the Postgres connection information. Make sure the TWW project is loaded.",
                level=Qgis.Critical,
            )

        self.logger.debug(
            f"dataprovider of vw_tww_wastewater_structure: {pg_layer.dataProvider().uri()}"
        )
        DatabaseUtils.databaseConfig.PGSERVICE = pg_layer.dataProvider().uri().service()
        DatabaseUtils.databaseConfig.PGHOST = pg_layer.dataProvider().uri().host()
        DatabaseUtils.databaseConfig.PGPORT = pg_layer.dataProvider().uri().port()
        DatabaseUtils.databaseConfig.PGDATABASE = pg_layer.dataProvider().uri().database()
        DatabaseUtils.databaseConfig.PGUSER = pg_layer.dataProvider().uri().username()
        DatabaseUtils.databaseConfig.PGPASS = pg_layer.dataProvider().uri().password()

    def _get_main_menu_action(self):
        actions = self.iface.pluginMenu().actions()
        result_actions = [action for action in actions if action.text() == self.main_menu_name]

        # OSX does not support & in the menu title
        if not result_actions:
            result_actions = [
                action
                for action in actions
                if action.text() == self.main_menu_name.replace("&", "")
            ]

        return result_actions[0]

    def update_admin_mode(self):

        admin_mode = QSettings().value("/TWW/AdminMode", False)
        # seems QGIS loads True as "true" on restart ?!
        if admin_mode and admin_mode != "false":
            admin_mode = True
            self.toolbar.addAction(self.importAction)
            self.toolbar.addAction(self.exportAction)
        else:
            self.toolbar.removeAction(self.importAction)
            self.toolbar.removeAction(self.exportAction)
            admin_mode = False

        self.enableSymbologyTriggersAction.setEnabled(admin_mode)
        self.disableSymbologyTriggersAction.setEnabled(admin_mode)
