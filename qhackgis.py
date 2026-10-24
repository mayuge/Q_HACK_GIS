
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import traceback
import winreg
import shutil

from pathlib import Path


APP_NAME = "QHackGIS"
APP_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
CONFIG_FILE = APP_DIR / "config.json"
LOG_DIR = APP_DIR / "logs"
LOG_FILE = LOG_DIR / "qhackgis.log"


def get_script_path() -> Path:
    """QGIS側で実行するスクリプトの場所を取得する。"""

    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve()

    # EXE内に同梱したスクリプトを、一時展開先ではない場所へ配置する。
    bundled_script = Path(sys._MEIPASS) / "qhackgis.py"
    runtime_dir = APP_DIR / "runtime"
    runtime_script = runtime_dir / "qhackgis.py"

    if not bundled_script.is_file():
        raise FileNotFoundError(
            f"EXE内のQGIS用スクリプトが見つかりません: {bundled_script}"
        )

    runtime_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(bundled_script, runtime_script)
    return runtime_script.resolve()


SCRIPT_PATH = get_script_path()


# ============================================================
# ログ
# ============================================================

def write_log(message: str) -> None:
    """ログをファイルとコンソールに出力する。"""

    line = f"[{APP_NAME}] {message}"
    print(line, flush=True)

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)

        with LOG_FILE.open("a", encoding="utf-8") as file:
            file.write(line + "\n")
    except OSError:
        pass


def log_exception() -> None:
    """現在の例外をログに保存する。"""

    write_log(traceback.format_exc())


# ============================================================
# QGIS環境の自動検出
# ============================================================

def get_qgis_candidates() -> list[Path]:
    """Windowsのレジストリと標準フォルダから候補を取得する。"""

    candidates: list[Path] = []

    def add(path) -> None:
        if not path:
            return

        try:
            value = os.path.expandvars(str(path).strip().strip('"'))
            candidate = Path(value).resolve()

            if candidate.exists() and candidate not in candidates:
                candidates.append(candidate)
        except (OSError, ValueError):
            pass

    # 環境変数
    for name in ("OSGEO4W_ROOT", "QGIS_PREFIX_PATH"):
        add(os.environ.get(name))

    # Windowsレジストリ
    registry_locations = [
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        ),
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
        ),
        (
            winreg.HKEY_CURRENT_USER,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        ),
    ]

    for hive, key_path in registry_locations:
        try:
            with winreg.OpenKey(hive, key_path) as parent:
                count = winreg.QueryInfoKey(parent)[0]

                for index in range(count):
                    try:
                        name = winreg.EnumKey(parent, index)

                        with winreg.OpenKey(parent, name) as key:
                            values = {}

                            for value_name in (
                                "DisplayName",
                                "InstallLocation",
                                "DisplayIcon",
                            ):
                                try:
                                    values[value_name] = (
                                        winreg.QueryValueEx(
                                            key, value_name
                                        )[0]
                                    )
                                except OSError:
                                    pass

                        display_name = str(
                            values.get("DisplayName", "")
                        ).lower()

                        if "qgis" not in display_name:
                            continue

                        add(values.get("InstallLocation"))

                        icon_path = values.get("DisplayIcon", "")
                        if icon_path:
                            icon_path = str(icon_path).strip('"').split(",")[0]
                            add(Path(icon_path).parent)

                    except OSError:
                        continue

        except OSError:
            continue

    # 一般的なインストール先
    base_directories = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
        Path(r"C:\OSGeo4W"),
        Path(r"C:\OSGeo4W64"),
        Path(r"C:\OSGeo4W32"),
    ]

    for base in base_directories:
        add(base)

        if not base.is_dir():
            continue

        try:
            for child in base.iterdir():
                if child.is_dir():
                    name = child.name.lower()

                    if "qgis" in name or name.startswith("osgeo4w"):
                        add(child)
        except OSError:
            continue

    return candidates


def find_installation(path) -> dict | None:
    """指定されたパスからQGISの起動環境を検出する。"""

    try:
        selected = Path(path).resolve()
    except (OSError, ValueError):
        return None

    bases = [selected, *list(selected.parents)[:5]]

    for base in bases:
        prefixes = [
            base / "apps" / "qgis",
            base / "apps" / "qgis-ltr",
            base / "apps" / "qgis-dev",
            base,
        ]

        apps_dir = base / "apps"

        if apps_dir.is_dir():
            try:
                for child in apps_dir.iterdir():
                    if (
                        child.is_dir()
                        and child.name.lower().startswith("qgis")
                    ):
                        prefixes.append(child)
            except OSError:
                pass

        prefix = next(
            (
                item for item in prefixes
                if (item / "python").is_dir()
                and (item / "resources").is_dir()
            ),
            None,
        )

        launchers = [
            base / "bin" / "python-qgis.bat",
            base / "bin" / "python-qgis-ltr.bat",
        ]

        if base.name.lower() == "bin":
            launchers.extend([
                base / "python-qgis.bat",
                base / "python-qgis-ltr.bat",
            ])

        launcher = next(
            (item for item in launchers if item.is_file()),
            None,
        )

        if prefix is not None and launcher is not None:
            return {
                "root": str(base),
                "prefix": str(prefix),
                "launcher": str(launcher),
            }

    return None


# ============================================================
# QGIS付属Pythonで実行
# ============================================================

def execute_qgis(
    installation: dict,
    mode: str,
    timeout: int = 90,
):
    """QGISの起動用バッチからQHackGISの処理を実行する。"""

    launcher = Path(installation["launcher"])
    root = installation["root"]

    if not launcher.is_file():
        raise FileNotFoundError(
            f"起動用バッチがありません: {launcher}"
        )

    batch_content = (
        "@echo off\r\n"
        f'call "{launcher}" "{SCRIPT_PATH}" '
        f'--qhackgis-{mode} "{root}"\r\n'
        "exit /b %errorlevel%\r\n"
    )

    fd, batch_name = tempfile.mkstemp(
        prefix="qhackgis_",
        suffix=".bat",
        text=True,
    )
    os.close(fd)

    batch_path = Path(batch_name)

    try:
        batch_path.write_text(batch_content, encoding="utf-8")

        result = subprocess.run(
            ["cmd.exe", "/d", "/c", str(batch_path)],
            cwd=str(SCRIPT_PATH.parent),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

        if result.stdout:
            write_log(f"標準出力:\n{result.stdout}")

        if result.stderr:
            write_log(f"エラー出力:\n{result.stderr}")

        write_log(
            f"実行モード={mode}, 終了コード={result.returncode}"
        )

        return result

    finally:
        try:
            batch_path.unlink(missing_ok=True)
        except OSError:
            pass


def test_qgis(installation: dict) -> bool:
    """PyQGISの初期化を検証する。"""

    write_log(f"QGISテスト: {installation['root']}")

    try:
        result = execute_qgis(installation, "test")
        return result.returncode == 0
    except Exception:
        log_exception()
        return False


def detect_qgis() -> dict | None:
    """検出した候補をテストし、利用できる環境を返す。"""

    checked = set()

    for candidate in get_qgis_candidates():
        installation = find_installation(candidate)

        if installation is None:
            continue

        key = installation["launcher"].lower()

        if key in checked:
            continue

        checked.add(key)
        write_log(f"QGIS候補: {installation['root']}")

        if test_qgis(installation):
            write_log("QGISの初期化に成功しました。")
            return installation

    return None


# ============================================================
# 設定の保存
# ============================================================

def save_config(installation: dict) -> None:
    """テストに成功した環境を保存する。"""

    APP_DIR.mkdir(parents=True, exist_ok=True)

    CONFIG_FILE.write_text(
        json.dumps(installation, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def load_config() -> dict | None:
    """保存済みの環境を読み込む。"""

    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return find_installation(data["root"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def launch_qhackgis(installation: dict) -> None:
    """独立したQHackGIS画面を起動する。"""

    launcher = Path(installation["launcher"])
    root = installation["root"]

    batch_content = (
        "@echo off\r\n"
        f'call "{launcher}" "{SCRIPT_PATH}" '
        f'--qhackgis-run "{root}"\r\n'
        "exit /b %errorlevel%\r\n"
    )

    fd, batch_name = tempfile.mkstemp(
        prefix="qhackgis_gui_",
        suffix=".bat",
        text=True,
    )
    os.close(fd)

    batch_path = Path(batch_name)
    batch_path.write_text(batch_content, encoding="utf-8")

    try:
        subprocess.Popen(
            ["cmd.exe", "/d", "/c", str(batch_path)],
            cwd=str(SCRIPT_PATH.parent),
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        batch_path.unlink(missing_ok=True)
        raise


# ============================================================
# 初回セットアップ（コンソール）
# ============================================================

def show_setup() -> None:
    """QGISを自動検出できない場合、コンソールでインストール先を尋ねる。"""

    print()
    print("=" * 58)
    print(f" {APP_NAME} - GISエンジンの設定")
    print("=" * 58)
    print("QGISの起動環境を自動検出できませんでした。")
    print("QGISのインストール先フォルダを入力してください。")
    print("例: C:\\Program Files\\QGIS 3.40.6")
    print("終了する場合は、何も入力せず Enter を押してください。")
    print()

    while True:
        try:
            selected_path = input("QGISのインストール先 > ").strip().strip('"')
        except (EOFError, KeyboardInterrupt):
            print("\\n設定を中止しました。")
            return

        if not selected_path:
            print("設定を中止しました。")
            return

        installation = find_installation(selected_path)

        if installation is None:
            print("指定された場所からQGISの起動環境を検出できませんでした。")
            print("QGISのインストール先、またはOSGeo4Wのルートフォルダを確認してください。")
            continue

        print(f"検出しました: {installation['root']}")
        print("QGISの動作を確認しています...")

        if not test_qgis(installation):
            print("起動テストに失敗しました。ログを確認してください:")
            print(f"  {LOG_FILE}")
            continue

        try:
            save_config(installation)
        except OSError as exc:
            write_log(f"設定保存エラー: {exc}")
            print(f"設定を保存できませんでした: {exc}")
            continue

        print("設定を保存しました。QHackGISを起動します。")
        launch_qhackgis(installation)
        return


# ============================================================
# PyQGIS初期化テスト
# ============================================================

def run_test(root_path: str) -> None:
    """QGIS付属PythonでPyQGISを初期化する。"""

    installation = find_installation(root_path)

    if installation is None:
        raise RuntimeError("QGIS環境が見つかりません。")

    from qgis.core import QgsApplication, Qgis, QgsVectorLayer

    QgsApplication.setPrefixPath(installation["prefix"], True)
    app = QgsApplication([], False)

    try:
        app.initQgis()

        layer = QgsVectorLayer(
            "Point?crs=EPSG:4326",
            "test",
            "memory",
        )

        if not layer.isValid():
            raise RuntimeError("メモリレイヤーの初期化に失敗しました。")

        print(f"QGIS version: {Qgis.QGIS_VERSION}")
        print("PyQGIS initialization: OK")

    finally:
        app.exitQgis()


# ============================================================
# 地図画面
# ============================================================

def run_qhackgis(root_path: str) -> None:
    """QGIS標準の地図キャンバスを利用してQHackGISを表示する。"""

    installation = find_installation(root_path)

    if installation is None:
        raise RuntimeError("QGIS環境が見つかりません。")

    from qgis.core import (
        QgsApplication,
        QgsCoordinateReferenceSystem,
        QgsCoordinateTransform,
        QgsPointXY,
        QgsProject,
        QgsRasterLayer,
        QgsVectorLayer,
        QgsRectangle,
    )
    from qgis.gui import QgsMapCanvas, QgsMapToolPan
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtWidgets import (
        QMainWindow,
        QDockWidget,
        QListWidget,
        QListWidgetItem,
        QStatusBar,
        QLabel,
        QWidget,
        QVBoxLayout,
        QHBoxLayout,
        QPushButton,
        QFileDialog,
        QInputDialog,
        QMessageBox,
    )

    QgsApplication.setPrefixPath(installation["prefix"], True)

    app = QgsApplication([], True)
    app.initQgis()

    class QHackGISWindow(QMainWindow):
        def __init__(self):
            super().__init__()

            self.setWindowTitle(APP_NAME)
            self.resize(1440, 900)
            self.setMinimumSize(900, 600)

            # 地図キャンバス
            self.canvas = QgsMapCanvas(self)
            # 描画品質とパフォーマンス
            self.canvas.enableAntiAliasing(False)
            self.canvas.setCachingEnabled(True)
            self.canvas.setParallelRenderingEnabled(True)
            self.canvas.setPreviewJobsEnabled(True)

            # 描画結果の更新間隔を短くする
            self.canvas.setMapUpdateInterval(30)

            self.canvas.setDestinationCrs(
                QgsCoordinateReferenceSystem("EPSG:3857")
            )

            # 左ドラッグによる地図移動
            self.pan_tool = QgsMapToolPan(self.canvas)
            self.canvas.setMapTool(self.pan_tool)

            # ホイールによるズーム
            self.canvas.setWheelFactor(2.0)

            self.setCentralWidget(self.canvas)

            # レイヤーコントローラー
            layer_panel = QWidget()
            layer_layout = QVBoxLayout(layer_panel)
            layer_layout.setContentsMargins(8, 8, 8, 8)
            layer_layout.setSpacing(6)

            self.layer_list = QListWidget()
            self.layer_list.setSelectionMode(QListWidget.SingleSelection)
            self.layer_list.itemChanged.connect(self.on_layer_item_changed)
            layer_layout.addWidget(self.layer_list)

            # レイヤー操作ボタン
            button_row = QHBoxLayout()
            self.add_layer_button = QPushButton("追加")
            self.remove_layer_button = QPushButton("削除")
            self.up_layer_button = QPushButton("上へ")
            self.down_layer_button = QPushButton("下へ")

            button_row.addWidget(self.add_layer_button)
            button_row.addWidget(self.remove_layer_button)
            layer_layout.addLayout(button_row)

            order_row = QHBoxLayout()
            order_row.addWidget(self.up_layer_button)
            order_row.addWidget(self.down_layer_button)
            layer_layout.addLayout(order_row)

            self.add_layer_button.clicked.connect(self.show_add_layer_menu)
            self.remove_layer_button.clicked.connect(self.remove_selected_layer)
            self.up_layer_button.clicked.connect(lambda: self.move_selected_layer(-1))
            self.down_layer_button.clicked.connect(lambda: self.move_selected_layer(1))

            self.layer_dock = QDockWidget("レイヤー", self)
            self.layer_dock.setWidget(layer_panel)
            self.layer_dock.setMinimumWidth(240)
            self.layer_dock.setMaximumWidth(340)
            self.addDockWidget(Qt.LeftDockWidgetArea, self.layer_dock)

            # 下部ステータスバー
            status_bar = QStatusBar(self)
            self.setStatusBar(status_bar)

            self.status_label = QLabel("準備完了")
            status_bar.addWidget(self.status_label, 1)

            self.scale_label = QLabel("")
            status_bar.addPermanentWidget(self.scale_label)

            # ベースマップ
            url = (
                "type=xyz&url="
                "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
            )

            self.osm_layer = QgsRasterLayer(
                url,
                "OpenStreetMap",
                "wms",
            )

            if not self.osm_layer.isValid():
                self.status_label.setText(
                    "ベースマップを読み込めませんでした。"
                )
                return

            QgsProject.instance().addMapLayer(self.osm_layer)
            self.add_layer_to_controller(self.osm_layer, checked=True)
            self.refresh_canvas_layers()

            # 初期表示位置
            source_crs = QgsCoordinateReferenceSystem("EPSG:4326")
            target_crs = QgsCoordinateReferenceSystem("EPSG:3857")

            transform = QgsCoordinateTransform(
                source_crs,
                target_crs,
                QgsProject.instance(),
            )

            center = transform.transform(
                QgsPointXY(139.7671, 35.6812)
            )

            self.canvas.setExtent(
                QgsRectangle(
                    center.x() - 15000,
                    center.y() - 15000,
                    center.x() + 15000,
                    center.y() + 15000,
                )
            )

            # 表示倍率が変わったときにステータスを更新する
            self.canvas.extentsChanged.connect(self.update_scale)
            self.update_scale()

            self.status_label.setText(
                "左ドラッグ: 移動   ホイール: ズーム"
            )

        def update_scale(self):
            """地図の縮尺表示を更新する。"""

            scale = self.canvas.scale()
            self.scale_label.setText(f"縮尺 1:{scale:,.0f}")

        def add_layer_to_controller(self, layer, checked=True):
            """レイヤーを一覧に登録する。"""

            item = QListWidgetItem(layer.name())
            item.setData(Qt.UserRole, layer.id())
            item.setFlags(
                item.flags()
                | Qt.ItemIsUserCheckable
                | Qt.ItemIsSelectable
                | Qt.ItemIsEnabled
            )
            item.setCheckState(Qt.Checked if checked else Qt.Unchecked)

            # itemChangedによる更新を抑えながら一覧へ追加する。
            self.layer_list.blockSignals(True)
            self.layer_list.insertItem(0, item)
            self.layer_list.blockSignals(False)
            self.layer_list.setCurrentItem(item)

        def on_layer_item_changed(self, _item):
            """チェック状態を地図の表示・非表示へ反映する。"""
            self.refresh_canvas_layers()

        def refresh_canvas_layers(self):
            """チェック済みレイヤーだけを地図へ反映する。"""
            project = QgsProject.instance()
            visible_layers = []

            for row in range(self.layer_list.count()):
                item = self.layer_list.item(row)
                layer_id = item.data(Qt.UserRole)
                layer = project.mapLayer(layer_id)

                if layer is not None and item.checkState() == Qt.Checked:
                    visible_layers.append(layer)

            self.canvas.setLayers(visible_layers)
            self.canvas.refresh()

        def show_add_layer_menu(self):
            """ファイルレイヤーまたはXYZタイルを追加する。"""
            choices = ["ファイルから追加", "XYZタイルを追加"]
            choice, accepted = QInputDialog.getItem(
                self,
                "レイヤーを追加",
                "追加するレイヤーの種類:",
                choices,
                0,
                False,
            )

            if not accepted:
                return

            if choice == choices[0]:
                self.add_file_layer()
            elif choice == choices[1]:
                self.add_xyz_layer()

        def add_file_layer(self):
            """ベクターまたはラスターのファイルを追加する。"""
            file_path, _ = QFileDialog.getOpenFileName(
                self,
                "GISデータを選択",
                str(Path.home()),
                "GISデータ (*.gpkg *.shp *.geojson *.json *.kml *.gml "
                "*.tif *.tiff *.vrt *.img *.png *.jpg *.jpeg);;"
                "すべてのファイル (*.*)",
            )

            if not file_path:
                return

            layer_name = Path(file_path).stem

            # 先にベクターレイヤーとして試し、無効ならラスターとして試す。
            layer = QgsVectorLayer(file_path, layer_name, "ogr")
            if not layer.isValid():
                layer = QgsRasterLayer(file_path, layer_name)

            if not layer.isValid():
                QMessageBox.warning(
                    self,
                    "レイヤーを追加できません",
                    "選択したファイルをGISレイヤーとして読み込めませんでした。",
                )
                return

            QgsProject.instance().addMapLayer(layer)
            self.add_layer_to_controller(layer, checked=True)
            self.refresh_canvas_layers()
            self.status_label.setText(f"追加しました: {layer.name()}")

        def add_xyz_layer(self):
            """XYZタイルのURLを指定して追加する。"""
            url, accepted = QInputDialog.getText(
                self,
                "XYZタイルを追加",
                "タイルURL（{z}/{x}/{y} を含む）:",
                text="https://tile.openstreetmap.org/{z}/{x}/{y}.png",
            )

            if not accepted or not url.strip():
                return

            url = url.strip()
            layer_name, accepted = QInputDialog.getText(
                self,
                "レイヤー名",
                "一覧に表示する名前:",
                text="XYZタイル",
            )

            if not accepted:
                return

            layer_name = layer_name.strip() or "XYZタイル"
            uri = "type=xyz&url=" + url
            layer = QgsRasterLayer(uri, layer_name, "wms")

            if not layer.isValid():
                QMessageBox.warning(
                    self,
                    "レイヤーを追加できません",
                    "URLが正しくないか、XYZタイルとして認識できませんでした。",
                )
                return

            QgsProject.instance().addMapLayer(layer)
            self.add_layer_to_controller(layer, checked=True)
            self.refresh_canvas_layers()
            self.status_label.setText(f"追加しました: {layer.name()}")

        def remove_selected_layer(self):
            """選択したレイヤーを一覧とプロジェクトから削除する。"""
            item = self.layer_list.currentItem()
            if item is None:
                return

            layer_id = item.data(Qt.UserRole)
            layer_name = item.text()
            row = self.layer_list.row(item)
            self.layer_list.takeItem(row)

            QgsProject.instance().removeMapLayer(layer_id)
            self.refresh_canvas_layers()
            self.status_label.setText(f"削除しました: {layer_name}")

        def move_selected_layer(self, direction):
            """選択レイヤーを一覧内で上下に移動する。"""
            row = self.layer_list.currentRow()
            target_row = row + direction

            if row < 0 or not (0 <= target_row < self.layer_list.count()):
                return

            item = self.layer_list.takeItem(row)
            self.layer_list.insertItem(target_row, item)
            self.layer_list.setCurrentRow(target_row)
            self.refresh_canvas_layers()

    window = QHackGISWindow()
    window.show()

    try:
        app.exec()
    finally:
        app.exitQgis()


# ============================================================
# エントリーポイント
# ============================================================

def main() -> None:
    # QGIS付属Pythonから呼び出されるモード
    if len(sys.argv) >= 3:
        if sys.argv[1] == "--qhackgis-test":
            run_test(sys.argv[2])
            return

        if sys.argv[1] == "--qhackgis-run":
            run_qhackgis(sys.argv[2])
            return

    write_log("QHackGISを開始します。")

    # 保存済み設定を再利用する
    installation = load_config()

    if installation and test_qgis(installation):
        launch_qhackgis(installation)
        return

    # 保存済み設定が使えない場合は自動検出する
    write_log("QGISを自動検出しています。")
    installation = detect_qgis()

    if installation:
        save_config(installation)
        launch_qhackgis(installation)
        return

    # 検出できない場合のみ設定画面を表示する
    show_setup()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        write_log(f"致命的なエラー: {exc}")
        log_exception()
        print()
        print(f"{APP_NAME}の起動に失敗しました。")
        print(f"エラー: {exc}")
        print(f"ログ: {LOG_FILE}")
        try:
            input("Enterキーを押すと終了します...")
        except (EOFError, KeyboardInterrupt):
            pass
