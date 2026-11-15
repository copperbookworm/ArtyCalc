# pycrefine: 6 places could not be reconstructed reliably; treat the source below as unverified there:
#   - stack_underflow: an operand is missing from the stack [in MapView._reload_flags at CALL (offset 1628); Python 3.14]
#   - stack_underflow: an operand is missing from the stack [in OcrWorker.run at STORE_FAST_STORE_FAST (offset 1710); Python 3.14]
#   - stack_underflow: an operand is missing from the stack [in OcrWorker.run at STORE_FAST_STORE_FAST (offset 1812); Python 3.14]
#   - ambiguous_expression: default values '(False, who)' could not be attached to the function [in MainWindow._build_panel at SET_FUNCTION_ATTRIBUTE (offset 5040); Python 3.14]
#   - ambiguous_expression: default values '(False, who)' could not be attached to the function [in MainWindow._build_panel at SET_FUNCTION_ATTRIBUTE (offset 5132); Python 3.14]
#   - stack_underflow: an operand is missing from the stack [in MainWindow._clear_rows at CALL (offset 252); Python 3.14]
"""Squad 间瞄火力助手（桌面版）

流程：选地图/武器 -> 在地图上点出阵地 -> 框选游戏里 CURRENT GRID 区域
     -> 按识别热键（默认 F1，可自定义/组合键）截屏识图 -> 浮窗显示 阵地/目标/角度/方位
        （角度那行：mil 的迫击炮写「密位」，火箭巢/车载炮这类有射角的武器写「射角」）。
"""

from __future__ import annotations
import ctypes
import json
import math
import os
import queue
import random
import shutil
import sys
import threading
import time
import traceback
from pathlib import Path
from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QRectF, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QCursor, QMouseEvent, QFont, QFontMetrics, QFontMetricsF, QGuiApplication, QImage, QImageReader, QKeySequence, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QButtonGroup, QComboBox, QDoubleSpinBox, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QKeySequenceEdit, QMainWindow, QMessageBox, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget
import barrage
import core
import docr
import flagzones
import posocr
import los
import mapsdata
import paths
import targetsdb
import vision
from overlay import Overlay
HERE = paths.app_dir()
CONFIG_PATH = paths.config_path()
KEY_FALLBACK = None if paths.frozen() else Path('D:\\mil-ai\\IFC\\deepseekAPI\\DS_key.txt')
RECON_DATA = ((paths.resource_dir() / 'data') / 'recon_targets.json')
CMD_DATA = ((paths.resource_dir() / 'data') / 'command_factions.json')

def load_recon_targets():
    """侦察目标表（敌方阵营 → 分类 → 型号）。生成脚本：tools/build_recon_targets.py。"""
    try:
        return json.loads(RECON_DATA.read_text(encoding='utf-8'))
    except Exception:
        return {'factions': [], 'common': {'label': '通用目标', 'items': []}}

def load_command_factions():
    """发布命令：我方阵营 → 部队（LO 编制）→ 可建造的功能建筑/防御工事/武器。
    生成脚本：tools/build_command_factions.py（从 squad-armor 包与 SquadCalc_res 提取）。"""
    try:
        return json.loads(CMD_DATA.read_text(encoding='utf-8'))
    except Exception as e:
        print(f"[cmd] 编制表打不开：{e}", flush=True)
        return {'factions': []}

PEND_TOTAL = 3
CIRCUM_NEAR = 60.0
CIRCUM_SPAN_K = 6.0
_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 2
_w32 = None

def _win32_clipboard():
    """惰性初始化 Win32 剪贴板 API（只在 Windows 上可用）。"""
    global _w32
    if _w32 is not None:
        return _w32
    if sys.platform != 'win32':
        _w32 = False
        return _w32
    try:
        from ctypes import wintypes
        u = ctypes.windll.user32
        k = ctypes.windll.kernel32
        u.OpenClipboard.argtypes = [wintypes.HWND]
        u.OpenClipboard.restype = wintypes.BOOL
        u.EmptyClipboard.restype = wintypes.BOOL
        u.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        u.SetClipboardData.restype = wintypes.HANDLE
        k.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        k.GlobalAlloc.restype = ctypes.c_void_p
        k.GlobalLock.argtypes = [ctypes.c_void_p]
        k.GlobalLock.restype = ctypes.c_void_p
        k.GlobalUnlock.argtypes = [ctypes.c_void_p]
        k.GlobalFree.argtypes = [ctypes.c_void_p]
        _w32 = (u, k)
        return _w32
    except Exception as e:
        print('[clip] Win32 剪贴板不可用:', e)
        _w32 = False
        return _w32

def set_clipboard_text(text, tries=10):
    """把 text 写进系统剪贴板（别人正占用时重试 ~0.5 s）。"""
    api = _win32_clipboard()
    if api:
        u, k = api
        for _ in range(tries):
            if u.OpenClipboard(None):
                try:
                    u.EmptyClipboard()
                    data = (text.encode('utf-16-le') + b'\x00\x00')
                    h = k.GlobalAlloc(_GMEM_MOVEABLE, len(data))
                    if not h:
                        return False
                    p = k.GlobalLock(h)
                    ctypes.memmove(p, data, len(data))
                    k.GlobalUnlock(h)
                    if not u.SetClipboardData(_CF_UNICODETEXT, h):
                        k.GlobalFree(h)
                        return False
                    try:
                        ctypes.windll.ole32.OleFlushClipboard()
                    except Exception:
                        pass
                    return True
                finally:
                    u.CloseClipboard()
            time.sleep(0.05)
        print('[clip] 剪贴板被别的程序占用，重试后仍失败')
    try:
        QApplication.clipboard().setText(text)
        return QApplication.clipboard().text() == text
    except Exception as e:
        print('[clip] 复制失败:', e)
        return False

MODE_TEXT = {'circum': '三点外心', 'centroid': '三点重心', 'mid': '两点中点', 'single1': '单次定位', 'single3': '三次一致', 'single2': '两次一致', 'grid': '坐标', 'tel': '望远镜'}

def combine_targets(pts):
    """把 2~3 个截到的坐标合成一个目标点。

    pts: [(rx, rz, 原文), ...]
    返回 (target, mode, msg)：
      * 三点互不相同 -> 三点外心（近似共线时退回三点重心）
      * 有两点的坐标相同 -> 相同的那两个算一个，与剩下的取中点
      * 三个都相同 -> 按单点
    """
    uniq = []
    for _item in pts:
        q = _item
        if any(((abs((q[0] - u[0])) < 1e-06 and abs((q[1] - u[1])) < 1e-06) for u in uniq)):
            continue
        uniq.append(q)
    codes = [q[2] for q in pts]
    if len(uniq) >= 3:
        (x1, z1), (x2, z2), (x3, z3) = [(u[0], u[1]) for u in uniq[:3]]
        d = (2.0 * (((x1 * (z2 - z3)) + (x2 * (z3 - z1))) + (x3 * (z1 - z2))))
        cx = cz = None
        if abs(d) > 1e-06:
            a1, a2, a3 = (x1 * x1) + (z1 * z1), (x2 * x2) + (z2 * z2), (x3 * x3) + (z3 * z3)
            cx = ((((a1 * (z2 - z3)) + (a2 * (z3 - z1))) + (a3 * (z1 - z2))) / d)
            cz = ((((a1 * (x3 - x2)) + (a2 * (x1 - x3))) + (a3 * (x2 - x1))) / d)
        gx, gz = ((x1 + x2) + x3) / 3.0, ((z1 + z2) + z3) / 3.0
        side = max(math.hypot((x1 - x2), (z1 - z2)), math.hypot((x2 - x3), (z2 - z3)), math.hypot((x3 - x1), (z3 - z1)))
        if cx is not None and math.hypot((cx - gx), (cz - gz)) <= max(CIRCUM_NEAR, (CIRCUM_SPAN_K * side)):
            return ((cx, cz), 'circum', ('三点外心：' + ' + '.join(codes)))
        return ((gx, gz), 'centroid', ('三点近似共线，外心过远，改用重心：' + ' + '.join(codes)))
    if len(uniq) == 2:
        (x1, z1), (x2, z2) = [(u[0], u[1]) for u in uniq]
        return ((((x1 + x2) / 2.0), ((z1 + z2) / 2.0)), 'mid', ('两点中点：' + ' + '.join(codes)))
    u = uniq[0]
    if len(pts) >= 3:
        return ((u[0], u[1]), 'single3', '{0} 个读数都是 {1}，按单点算'.format(len(pts), u[2]))
    return ((u[0], u[1]), 'single2', '两次都是 {0}，按单点算'.format(u[2]))

def load_config():
    cfg = {'api_key': '', 'region': [100, 100, 320, 110], 'map': 'yehorivka', 'weapon': 'Mortar#default', 'arc': 'auto', 'hotkey': 'F1', 'hotkey_exact': 'F2', 'hotkey_clear': 'F3', 'hotkey_overlay': 'F4', 'overlay': None, 'regions': [], 'region_name': '', 'd_region_az_cmd': None, 'd_region_az_scout': None, 'drone_who': 'cmd', 'region_az': None, 'tel_on': False, 'arty_sub': 'drone', 's_region': None, 's_region_az': None, 's_regions': [], 's_region_name': '', 's_hotkey': 'F8', 's_clear': 'F9', 's_overlay': 'F10', 'mode': 'arty', 'r_region': None, 'r_region_az': None, 'r_region_tgt': None, 'r_region_tgt2': None, 'recon_src': 'cmd', 'recon_sub': 'scope', 'r_faction': '', 'r_hotkey': 'F5', 'r_report': 'F11', 'r_clear': 'F6', 'r_overlay': 'F7', 'c_faction': '', 'c_unit': '', 'c_hotkey': 'Alt+F12', 'c_clear': 'Alt+Shift+F12', 'c_overlay': 'Ctrl+F12', 'bar_positions': [], 'bar_checked': [], 'bar_sub': 'drone', 'bar_gun': '', 'b_hotkey': 'Ctrl+F1', 'b_clear': 'Ctrl+Shift+F1', 'b_overlay': 'Ctrl+Alt+F1'}
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding='utf-8')))
        except Exception as e:
            # 配置读坏了以前是静默吞掉：用户所有设置悄悄回到默认，还查不到原因。
            # 现在留一份 .bad 备份，并在 run.log / 控制台写明。
            bad = CONFIG_PATH.with_suffix(CONFIG_PATH.suffix + '.bad')
            try:
                shutil.copyfile(CONFIG_PATH, bad)
            except Exception:
                bad = None
            print(f"[cfg] 配置文件读不了（{e}）· 用默认配置启动 · 坏文件备份：{bad}", flush=True)
    if not cfg.get('api_key') and KEY_FALLBACK is not None and KEY_FALLBACK.exists():
        try:
            cfg['api_key'] = KEY_FALLBACK.read_text(encoding='utf-8').strip()
        except Exception:
            pass
    migrate_v120_regions(cfg)
    migrate_v130_split(cfg)
    cfg['bar_positions'] = barrage.normalize_positions(cfg.get('bar_positions'))
    return cfg

def migrate_v120_regions(cfg):
    """1.2.0 的 bug：侦察的「方位角区域」被写进了间瞄的「坐标区域」。
    若「坐标区域」小得像一行方位角数字（<=90 x 40），而侦察方位角又没设，
    就把它挪回 r_region_az，并按当前预设名把间瞄坐标区域还原。"""
    r = cfg.get('region')
    if (not r or cfg.get('r_region_az')):
        return False
    try:
        x, y, w, h = [int(v) for v in r]
        if w > 90 or h > 40:
            return False
        cfg['r_region_az'] = [x, y, w, h]
        name = (cfg.get('region_name') or '')
        for p in (cfg.get('regions') or []):
            if not p.get('name') == name:
                continue
            if not p.get('rect'):
                continue
            cfg['region'] = list(p['rect'])
            break
        print(f"[cfg] 修正 1.2.0 的区域错位：{w}x{h} 的方位角框 -> r_region_az，坐标区域 -> {cfg['region']}", flush=True)
        return True
    except Exception:
        return False

def migrate_v130_split(cfg):
    """1.3.0：望远镜从「间瞄里的一个开关」升级成独立子模式（截图框、预设、键位都分开）。
    老配置的 region_az 挪到 s_region_az；玩家坐标框沿用原来的 region（同一个 HUD 位置）。"""
    changed = False
    if cfg.get('arty_sub') not in ('drone', 'scope'):
        cfg['arty_sub'] = 'drone'
        changed = True
    if not cfg.get('s_region_az') and cfg.get('region_az'):
        cfg['s_region_az'] = list(cfg['region_az'])
        changed = True
    if not cfg.get('s_region') and cfg.get('region'):
        cfg['s_region'] = list(cfg['region'])
        changed = True
    if changed:
        print(f"[cfg] 1.3.0 子模式：arty_sub={cfg['arty_sub']}，望远镜区域 {cfg.get('s_region_az')} / {cfg.get('s_region')}", flush=True)
    return changed

SELF_TEST = False
# 自测失败登记：以前自测只把 FAIL 打印出来、退出码仍是 0，跑脚本的人（和 CI）
# 分不出「真过了」还是「挂了一半」——这里记一笔，main() 退场时据此返回非 0。
SELF_FAILS = []

def self_fail(name):
    """记一条自测失败（一行里带 FAIL 的那条），退场时 main() 会返回 1。"""
    SELF_FAILS.append(str(name))
    return None

def self_fail_text():
    return ', '.join(SELF_FAILS[:6]) + (' …' if len(SELF_FAILS) > 6 else '')

def save_config(cfg):
    if SELF_TEST:
        return None
    tmp = CONFIG_PATH.with_suffix(CONFIG_PATH.suffix + '.tmp')
    try:
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(tmp, CONFIG_PATH)  # 原子替换：写到一半崩掉也不会留下截断的 config.json
    except Exception as e:
        print(f"[cfg] 保存配置失败（{e}）", flush=True)
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass

class MapDecode(QThread):
    """后台解一张高清底图。8192² 的 JPEG 解一次约 1.3 s，放主线程会把界面卡住。

    done 里回带 generation：切图快的时候旧线程解完就丢掉，不会把旧图贴到新地图上。
    """
    done = Signal(int, object)
    def __init__(self, path, gen, parent=None):
        super().__init__(parent)
        self._path = path
        self._gen = gen
        self.setObjectName('mapdecode')

    def run(self):
        try:
            q = QImage(self._path)
        except Exception:
            return None
        else:
            self.done.emit(self._gen, q)

class FlagPanel(QFrame):
    """地图右上角的「旗点 · 占领区」面板。

    和 APK 里的面板一一对应：图层下拉、进攻链下拉（只有 RAAS 有）、
    占领区/中文名/英文名/序号四个开关、旗点列表、选中旗点的说明。
    这里只管界面，选了什么、点了哪一行都发信号给 MapView；数据与画图都在 MapView。
    """
    layer_changed = Signal(str)
    lane_changed = Signal(str)
    opt_changed = Signal(str, bool)
    pick = Signal(object)
    closed = Signal()
    QSS = """
QFrame#flagPanel{background:rgba(9,13,19,238);border:1px solid rgba(255,178,90,150);
                 border-radius:8px;}
QFrame#flagPanel QLabel{color:#cfe3f5;font-size:12px;}
QFrame#flagPanel QComboBox{background:rgba(20,28,38,240);
                 border:1px solid rgba(90,120,150,190);border-radius:4px;
                 color:#dbe9f7;font-size:12px;padding:1px 6px;}
QFrame#flagPanel QComboBox QAbstractItemView{background:#0d141c;color:#dbe9f7;
                 selection-background-color:#17323f;}
QFrame#flagPanel QPushButton#flagOpt{background:rgba(10,16,24,190);
                 border:1px solid rgba(90,120,150,170);border-radius:4px;
                 color:#9fb6cf;font-size:11px;padding:2px 6px;}
QFrame#flagPanel QPushButton#flagOpt:hover{border-color:#2f7d94;color:#9fe6f5;}
QFrame#flagPanel QPushButton#flagOpt:checked{background:#17323f;
                 border-color:#2f7d94;color:#9fe6f5;}
QFrame#flagPanel QPushButton#flagClose{background:transparent;border:none;
                 color:#8aa3b8;font-size:14px;}
QFrame#flagPanel QPushButton#flagClose:hover{color:#ff8f6b;}
QFrame#flagPanel QListWidget{background:rgba(6,10,16,205);
                 border:1px solid rgba(90,120,150,120);border-radius:4px;
                 color:#cfe3f5;font-size:11px;}
QFrame#flagPanel QListWidget::item{padding:3px 5px;}
QFrame#flagPanel QListWidget::item:selected{background:#17323f;color:#9fe6f5;}
"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('flagPanel')
        self.setStyleSheet(self.QSS)
        self.setFrameShape(QFrame.NoFrame)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(6)
        top = QHBoxLayout()
        top.setSpacing(4)
        title = QLabel('旗点 · 占领区')
        title.setStyleSheet('color:#ffd166;font-weight:700;font-size:12px;')
        top.addWidget(title)
        top.addStretch(1)
        self.btn_close = QPushButton('×')
        self.btn_close.setObjectName('flagClose')
        self.btn_close.setFixedSize(20, 20)
        self.btn_close.setCursor(Qt.PointingHandCursor)
        self.btn_close.setToolTip('收起旗点面板（地图上的旗点也一起关掉）')
        self.btn_close.clicked.connect(self.closed.emit)
        top.addWidget(self.btn_close)
        lay.addLayout(top)
        row = QHBoxLayout()
        row.setSpacing(4)
        row.addWidget(QLabel('图层'))
        self.cb_layer = QComboBox()
        self.cb_layer.currentIndexChanged.connect(self._on_layer)
        row.addWidget(self.cb_layer, 1)
        lay.addLayout(row)
        self.lb_lane = QLabel('进攻链')
        self.cb_lane = QComboBox()
        self.cb_lane.currentIndexChanged.connect(self._on_lane)
        lane_row = QHBoxLayout()
        lane_row.setSpacing(4)
        lane_row.addWidget(self.lb_lane)
        lane_row.addWidget(self.cb_lane, 1)
        lay.addLayout(lane_row)
        opts = QHBoxLayout()
        opts.setSpacing(4)
        self.opt = {}
        for key, text in (('zone', '占领区'), ('cn', '中文名'), ('en', '英文名'), ('seq', '序号')):
            b = QPushButton(text)
            b.setObjectName('flagOpt')
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.toggled.connect((lambda on, k=key: self.opt_changed.emit(k, bool(on))))
            opts.addWidget(b)
            self.opt[key] = b
        lay.addLayout(opts)
        self.list = QListWidget()
        self.list.setUniformItemSizes(True)
        self.list.setToolTip('点一行就把地图居中到那个旗点')
        self.list.itemClicked.connect(self._on_pick)
        lay.addWidget(self.list, 1)
        self.lb_info = QLabel('')
        self.lb_info.setWordWrap(True)
        self.lb_info.setStyleSheet('color:#ffe9a8;font-size:11px;')
        lay.addWidget(self.lb_info)
        self.lb_note = QLabel('')
        self.lb_note.setWordWrap(True)
        self.lb_note.setStyleSheet('color:#8aa3b8;font-size:11px;')
        lay.addWidget(self.lb_note)

    def set_layers(self, entries):
        self.cb_layer.blockSignals(True)
        self.cb_layer.clear()
        for key, label in (entries or []):
            self.cb_layer.addItem(str(label), str(key))
        self.cb_layer.blockSignals(False)

    def set_lanes(self, entries):
        self.cb_lane.blockSignals(True)
        self.cb_lane.clear()
        for key, label in (entries or []):
            self.cb_lane.addItem(str(label), str(key))
        self.cb_lane.blockSignals(False)

    def set_lane_visible(self, on):
        self.lb_lane.setVisible(bool(on))
        self.cb_lane.setVisible(bool(on))

    def set_current(self, layer=None, lane=None):
        if layer is not None:
            i = self.cb_layer.findData(str(layer))
            if i >= 0:
                self.cb_layer.blockSignals(True)
                self.cb_layer.setCurrentIndex(i)
                self.cb_layer.blockSignals(False)
        if lane is not None:
            i = self.cb_lane.findData(str(lane))
            if i >= 0:
                self.cb_lane.blockSignals(True)
                self.cb_lane.setCurrentIndex(i)
                self.cb_lane.blockSignals(False)
                return None
            return None

    def set_opts(self, zone=True, cn=True, en=False, seq=True):
        for key, val in (('zone', zone), ('cn', cn), ('en', en), ('seq', seq)):
            b = self.opt[key]
            b.blockSignals(True)
            b.setChecked(bool(val))
            b.blockSignals(False)

    def set_items(self, items, all_lanes=False):
        self.list.clear()
        for it in (items or []):
            if it.get('main'):
                col = '#9fb6cf'
            else:
                if all_lanes:
                    pass
                col = flagzones.lane_color(it['tags'][0]) if it.get('tags') else '#ffb347'
            txt = f"{str((it.get('tag') or '')):>3}  {(it.get('cn') or it.get('name'))}"
            row = QListWidgetItem(txt)
            row.setData(Qt.UserRole, it.get('k'))
            row.setForeground(QColor(col))
            row.setToolTip(flagzones.info_text(it, all_lanes))
            self.list.addItem(row)

    def select_k(self, k):
        hit = False
        self.list.blockSignals(True)
        for i in range(self.list.count()):
            row = self.list.item(i)
            if not row.data(Qt.UserRole) == k:
                continue
            self.list.setCurrentItem(row)
            hit = True
            break
        if not hit:
            self.list.clearSelection()
        self.list.blockSignals(False)
        if hit and self.list.currentItem() is not None:
            self.list.scrollToItem(self.list.currentItem())
            return None
        return None

    def clear_sel(self):
        self.list.blockSignals(True)
        self.list.clearSelection()
        self.list.setCurrentRow(-1)
        self.list.blockSignals(False)
        self.lb_info.setText('')

    def set_info(self, text):
        self.lb_info.setText((text or ''))

    def set_note(self, text):
        self.lb_note.setText((text or ''))

    def _on_layer(self, _idx):
        key = self.cb_layer.currentData()
        if key:
            self.layer_changed.emit(str(key))
            return None

    def _on_lane(self, _idx):
        key = self.cb_lane.currentData()
        if key:
            self.lane_changed.emit(str(key))
            return None

    def _on_pick(self, item):
        if item is not None:
            self.pick.emit(item.data(Qt.UserRole))
            return None

class MapView(QWidget):
    gun_set = Signal(float, float)
    locate_requested = Signal()
    grid_toggled = Signal(bool)
    hd_ready = Signal()
    flag_notice = Signal(str)
    timer_clicked = Signal()
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(520, 420)
        self.setCursor(Qt.CrossCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.map = None
        self.img = None
        self.img_lo = None
        self._map_note = ''
        self._gen = 0
        self._dec = None
        self._pending = None
        self.scale = 0.1
        self.ox = 0.0
        self.oy = 0.0
        self.gun = None
        self.target = None
        self.player = None
        self.buddy = None
        self.grid_level = 3
        self.target_title = '目标'
        self._press = None
        self._moved = False
        self._fit_pending = True
        self._user_zoom = False
        self.show_grid = True
        self.pin = None
        self.pin_text = ''
        self.hover = None
        self.flags_on = False
        self.flag_layer = None
        self.flag_lane = flagzones.ALL
        self.flag_items = []
        self.flag_sel = None
        self.flag_show_zone = True
        self.flag_show_cn = True
        self.flag_show_en = False
        self.flag_show_seq = True
        _btn_css = 'QPushButton{background:rgba(10,16,24,190);border:1px solid rgba(90,120,150,190);border-radius:5px;color:#cfe3f5;font-size:12px;}QPushButton:hover{background:rgba(20,40,56,220);border-color:#2f7d94;color:#9fe6f5;}QPushButton:checked{background:#17323f;border-color:#2f7d94;color:#9fe6f5;}'
        self.btn_locate = QPushButton('查坐标', self)
        self.btn_locate.setToolTip('输入 4 级网格坐标，直接把地图定位过去（例 B11-2-5-6）')
        self.btn_locate.setFixedSize(64, 28)
        self.btn_locate.setCursor(Qt.PointingHandCursor)
        self.btn_locate.setStyleSheet(_btn_css)
        self.btn_locate.clicked.connect((lambda: self.locate_requested.emit()))
        self.btn_grid = QPushButton('网格', self)
        self.btn_grid.setCheckable(True)
        self.btn_grid.setChecked(True)
        self.btn_grid.setToolTip('显示/隐藏 4 级网格（300 m 主格 + 三级 3×3 子格）')
        self.btn_grid.setFixedSize(64, 28)
        self.btn_grid.setCursor(Qt.PointingHandCursor)
        self.btn_grid.setStyleSheet(_btn_css)
        self.btn_grid.toggled.connect(self.grid_toggled.emit)
        self.btn_flags = QPushButton('旗点', self)
        self.btn_flags.setCheckable(True)
        self.btn_flags.setToolTip('显示/隐藏旗点与占领区（AAS / RAAS / Invasion）；点旗点看中文名、占领区形状与格号')
        self.btn_flags.setFixedSize(64, 28)
        self.btn_flags.setCursor(Qt.PointingHandCursor)
        self.btn_flags.setStyleSheet(_btn_css)
        self.btn_flags.toggled.connect(self.set_flags_on)
        self.btn_timer = QPushButton('计时', self)
        self.btn_timer.setCheckable(True)
        self.btn_timer.setToolTip('开始计时（从 00:00:00 往上走，不能暂停）；再点一次结束 —— 计时期间时间显示在浮窗第一行')
        self.btn_timer.setFixedSize(64, 28)
        self.btn_timer.setCursor(Qt.PointingHandCursor)
        self.btn_timer.setStyleSheet(_btn_css)
        self.btn_timer.clicked.connect((lambda: self.timer_clicked.emit()))
        self.panel = FlagPanel(self)
        self.panel.hide()
        self.panel.layer_changed.connect(self.set_flag_layer)
        self.panel.lane_changed.connect(self.set_flag_lane)
        self.panel.opt_changed.connect(self._on_flag_opt)
        self.panel.pick.connect(self.focus_flag)
        self.panel.closed.connect(self._close_flag_panel)
        self._place_btns()

    def w2s(self, rx, rz):
        return ((self.ox + (rx * self.scale)), (self.oy + (rz * self.scale)))

    def s2w(self, sx, sy):
        return (((sx - self.ox) / self.scale), ((sy - self.oy) / self.scale))

    def set_map(self, m):
        self.map = m
        self._gen = (getattr(self, '_gen', 0) + 1)
        gen = self._gen
        self.img = None
        self.img_lo = None
        self._map_note = ''
        if m is not None:
            try:
                if m.hd:
                    q = self._quick_image(m)
                    if q.isNull():
                        raise OSError(f"读不出底图：{m.basemap_path}")
                    self.img_lo = q
                    self.img = q
                    self._start_decode(m, gen)
                else:
                    pil = m.basemap()
                    q = QImage(pil.tobytes(), pil.width, pil.height, QImage.Format_RGB888).copy()
                    m.drop_cache()
                    if q.isNull():
                        raise OSError(f"读不出底图：{m.basemap_path}")
                    self.img_lo = q
                    self.img = q
            except Exception as e:
                self.img = None
                self.img_lo = None
                self._map_note = str(e)
                print(f"[map] 底图加载失败：{e}", flush=True)
        self._fit_pending = True
        self._user_zoom = False
        self.gun = None
        self.target = None
        self.player = None
        self.pin = None
        self.pin_text = ''
        self.hover = None
        self.target_title = '目标'
        self.flag_layer = None
        self.flag_lane = flagzones.ALL
        self.flag_items = []
        self.flag_sel = None
        self._reload_flags()
        self.fit()
        self.update()

    def fit(self):
        if not self.map:
            return None
        s = (min((self.width() / self.map.spanx), (self.height() / self.map.spanz)) * 0.98)
        self.scale = max(0.01, s)
        self.ox = ((self.width() - (self.map.spanx * self.scale)) / 2)
        self.oy = ((self.height() - (self.map.spanz * self.scale)) / 2)
        self._fit_pending = False
        self.update()

    def _quick_image(self, m):
        """先顶上的快图：库里的 2048² 缩略图 > 缩放读原图（老库没缩略图时）。"""
        lo = m.lo_path
        if lo is not None:
            q = QImage(str(lo))
            if not q.isNull():
                return q
        r = QImageReader(str(m.basemap_path))
        sz = r.size()
        if sz.isValid() and max(sz.width(), sz.height()) > 2048:
            r.setScaledSize(sz.scaled(2048, 2048, Qt.KeepAspectRatio))
        return r.read()

    def _start_decode(self, m, gen):
        self._pending = (str(m.basemap_path), gen)
        self._pump_decode()

    def _pump_decode(self):
        if self._dec is not None:
            if self._dec.isRunning():
                return None
            old = self._dec
            self._dec = None
            old.deleteLater()
        req = self._pending
        self._pending = None
        if not req:
            return None
        t = MapDecode(req[0], req[1], self)
        t.done.connect(self._on_decoded)
        t.finished.connect(self._on_decode_finished)
        self._dec = t
        t.start()

    def _on_decode_finished(self):
        t = self.sender()
        if t is self._dec:
            self._dec = None
            t.deleteLater()
        self._pump_decode()

    def _on_decoded(self, gen, q):
        if (gen != self._gen or q is None or q.isNull()):
            return None
        self.img = q
        self.update()
        self.hd_ready.emit()

    def wait_decodes(self, ms=4000):
        """退出前等后台解码收尾（QThread 还在跑就被销毁会直接崩）。

    排队等待的下一张也一起解完，别让界面停在小缩略图上；实在等不到就放手。
    """
        for _ in range(8):
            t = self._dec
            if t is not None:
                try:
                    t.wait(ms)
                except Exception:
                    break
            self._pump_decode()
            if self._dec is not None:
                continue
            if self._pending:
                continue
            break
        self._pending = None

    def _level_img(self):
        """选底图档位：屏幕 1 px 要采 >8 个底图像素时用 1/4 缩略图（8192² 整图缩放太慢）。"""
        if self.img_lo is None or self.map is None or self.img is None:
            return self.img
        need = ((self.img.width() / max(self.map.spanx, 1.0)) / max(self.scale, 1e-06))
        if need > 8.0:
            return self.img_lo
        return self.img

    def zoom_at(self, sx, sy, factor):
        if not self.map:
            return None
        rx, rz = self.s2w(sx, sy)
        self._user_zoom = True
        self.scale = min(max((self.scale * factor), 0.02), 6.0)
        self.ox = (sx - (rx * self.scale))
        self.oy = (sy - (rz * self.scale))
        self.update()

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(10, 12, 16))
        if self.map:
            if self.img is None:
                p.setPen(QColor(230, 140, 120) if self._map_note else QColor(120, 130, 145))
                p.setFont(QFont('Microsoft YaHei UI', 12))
                if self._map_note:
                    p.drawText(self.rect().adjusted(40, 0, -40, 0), (Qt.AlignCenter | Qt.TextWordWrap), (('底图读不出来：' + self._map_note) + "\n\n（高清地图库 mapsdb 要是丢了，把它放回程序目录；没有库时只认源码树里的 data\\maps）"))
                    return None
                p.drawText(self.rect(), Qt.AlignCenter, '选择地图后在这里点击设置阵地')
                return None
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        wx0, wz0 = self.s2w(0, 0)
        wx1, wz1 = self.s2w(self.width(), self.height())
        wx0, wx1 = max(0.0, wx0), min(self.map.spanx, wx1)
        wz0, wz1 = max(0.0, wz0), min(self.map.spanz, wz1)
        if wx1 > wx0 and wz1 > wz0:
            img = self._level_img()
            iw, ih = img.width(), img.height()
            src = QRectF(((wx0 / self.map.spanx) * iw), ((wz0 / self.map.spanz) * ih), (((wx1 - wx0) / self.map.spanx) * iw), (((wz1 - wz0) / self.map.spanz) * ih))
            ax, ay = self.w2s(wx0, wz0)
            bx, by = self.w2s(wx1, wz1)
            p.drawImage(QRectF(ax, ay, (bx - ax), (by - ay)), img, src)
        if self.show_grid:
            self.draw_grid(p)
        if self.flags_on:
            self._paint_flags(p)
        if self.target:
            tx, ty = self.w2s(*self.target)
            if self.gun:
                gx, gy = self.w2s(*self.gun)
                p.setPen(QPen(QColor(255, 178, 90, 190), 2, Qt.DashLine))
                p.drawLine(QPointF(gx, gy), QPointF(tx, ty))
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(90, 220, 240))
            p.drawEllipse(QPointF(tx, ty), 6, 6)
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(90, 220, 240), 1.5))
            p.drawEllipse(QPointF(tx, ty), 12, 12)
            _htg = self.htg if getattr(self, 'htg', None) is not None else self.map.h_at(*self.target)
            _tdz = getattr(self, 'tgt_dz', 0.0) or 0.0
            _ttl = f"{self.target_title} {self.map.grid_at(*self.target, self.grid_level)} · {_htg:.0f} m"
            if _tdz >= 0.5:
                _ttl += f" · +{_tdz:.0f} m"
            self._tag(p, (tx + 12), (ty - 26), _ttl, QColor(90, 220, 240))
        if self.gun:
            gx, gy = self.w2s(*self.gun)
            p.setPen(QPen(QColor(255, 209, 102), 2))
            p.setBrush(QColor(255, 209, 102))
            p.drawEllipse(QPointF(gx, gy), 6, 6)
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QPointF(gx, gy), 13, 13)
            _hg = self.hgun if getattr(self, 'hgun', None) is not None else self.map.h_at(*self.gun)
            _dz = getattr(self, 'gun_dz', 0.0) or 0.0
            self._tag(p, (gx + 12), (gy + 6), f"阵地 {self.map.grid_at(*self.gun, 3)} · {_hg:.0f} m" + (f" · +{_dz:.0f} m" if _dz >= 0.5 else ""), QColor(255, 209, 102))
        if self.player:
            px, py = self.w2s(*self.player)
            if self.target:
                tx, ty = self.w2s(*self.target)
                p.setPen(QPen(QColor(126, 227, 139, 170), 1.5, Qt.DashLine))
                p.drawLine(QPointF(px, py), QPointF(tx, ty))
            p.setPen(QPen(QColor(126, 227, 139), 2))
            p.setBrush(QColor(126, 227, 139))
            p.drawEllipse(QPointF(px, py), 5, 5)
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QPointF(px, py), 11, 11)
            self._tag(p, (px + 12), (py + 4), ('我 ' + self.map.grid_at(*self.player, self.grid_level)), QColor(126, 227, 139))
        if self.buddy:
            bx, by = self.w2s(*self.buddy)
            if self.target:
                tx, ty = self.w2s(*self.target)
                p.setPen(QPen(QColor(140, 200, 255, 170), 1.5, Qt.DashLine))
                p.drawLine(QPointF(bx, by), QPointF(tx, ty))
            p.setPen(QPen(QColor(140, 200, 255), 2))
            p.setBrush(QColor(140, 200, 255))
            p.drawEllipse(QPointF(bx, by), 5, 5)
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QPointF(bx, by), 11, 11)
            self._tag(p, (bx + 12), (by + 4), ('队友 ' + self.map.grid_at(*self.buddy, 4)), QColor(140, 200, 255))
        if self.pin:
            qx, qy = self.w2s(*self.pin)
            p.setPen(QPen(QColor(214, 130, 255), 2))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QPointF(qx, qy), 10, 10)
            p.drawLine(QPointF((qx - 16), qy), QPointF((qx + 16), qy))
            p.drawLine(QPointF(qx, (qy - 16)), QPointF(qx, (qy + 16)))
            self._tag(p, (qx + 14), (qy + 6), (self.pin_text or '定位'), QColor(214, 130, 255))
        self._scalebar(p)
        if self.hover:
            _txt = (self.map.grid_at(*self.hover, 4) + f" · {self.map.h_at(*self.hover):.0f} m")
            p.setFont(QFont('Consolas', 9))
            _w = (p.fontMetrics().horizontalAdvance(_txt) + 16)
            _r = QRectF(((self.width() - _w) - 12), (self.height() - 56), _w, 22)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(8, 12, 18, 200))
            p.drawRoundedRect(_r, 4, 4)
            p.setPen(QColor(200, 225, 245))
            p.drawText(_r, Qt.AlignCenter, _txt)
            return None

    def _grid_styles(self):
        """四档网格：300 m 主格 + 100 m + 100/3 m + 100/9 m，各带最小可见像素。"""
        return ((300.0, 22.0, QColor(130, 200, 255, 130), 1.2, Qt.SolidLine), (100.0, 34.0, QColor(130, 200, 255, 70), 1.0, Qt.DotLine), (33.333333333333336, 30.0, QColor(140, 210, 255, 55), 1.0, Qt.DotLine), (11.11111111111111, 26.0, QColor(160, 225, 255, 45), 1.0, Qt.DotLine))

    def draw_grid(self, p):
        if not self.map:
            return None
        spanx, spanz = self.map.spanx, self.map.spanz
        rx0, rz0 = self.s2w(0, 0)
        rx1, rz1 = self.s2w(self.width(), self.height())
        x_lo, x_hi = max(0.0, rx0), min(spanx, rx1)
        z_lo, z_hi = max(0.0, rz0), min(spanz, rz1)
        py_lo = max(0.0, self.oy)
        py_hi = min(float(self.height()), (self.oy + (spanz * self.scale)))
        px_lo = max(0.0, self.ox)
        px_hi = min(float(self.width()), (self.ox + (spanx * self.scale)))
        p.save()
        for step, min_px, color, width, style in self._grid_styles():
            if (step * self.scale) < min_px:
                continue
            p.setPen(QPen(color, width, style))
            a = (math.floor((x_lo / step)) * step)
            while a <= (x_hi + 1e-06):
                sx, _ = self.w2s(a, 0)
                p.drawLine(QPointF(sx, py_lo), QPointF(sx, py_hi))
                a += step
            b = (math.floor((z_lo / step)) * step)
            while b <= (z_hi + 1e-06):
                _, sy = self.w2s(0, b)
                p.drawLine(QPointF(px_lo, sy), QPointF(px_hi, sy))
                b += step
        if (300.0 * self.scale) >= 56.0:
            p.setFont(QFont('Consolas', 8))
            p.setPen(QColor(150, 215, 255, 210))
            b = 0.0
            while b < spanz:
                a = 0.0
                while a < spanx:
                    sx, sy = self.w2s(a, b)
                    if -40 <= sx <= self.width() and -20 <= sy <= self.height():
                        p.drawText(QPointF((sx + 3), (sy + 13)), core.text_at((a + 150.0), (b + 150.0), 1))
                    a += 300.0
                b += 300.0
        p.restore()

    def focus_point(self, rx, rz, scale=3.2):
        """把地图居中放大到某个相对坐标（「查坐标」用）。"""
        if not self.map:
            return None
        self._user_zoom = True
        self.scale = min(max(scale, 0.02), 6.0)
        self.ox = ((self.width() / 2) - (rx * self.scale))
        self.oy = ((self.height() / 2) - (rz * self.scale))
        self.update()

    def set_pin(self, pt, text=''):
        self.pin = (float(pt[0]), float(pt[1]))
        self.pin_text = text or ''
        self.update()

    def clear_pin(self):
        self.pin = None
        self.pin_text = ''
        self.update()

    def _hover_key(self, pt):
        if not pt or not self.map:
            return None
        try:
            return self.map.grid_at(pt[0], pt[1], 4)
        except Exception:
            return None

    def _tag(self, p, x, y, text, color):
        f = QFont('Microsoft YaHei UI', 9)
        p.setFont(f)
        w = (p.fontMetrics().horizontalAdvance(text) + 14)
        r = QRectF(x, y, w, 22)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(8, 12, 18, 210))
        p.drawRoundedRect(r, 4, 4)
        p.setPen(color)
        p.drawText(r, Qt.AlignCenter, text)

    def _scalebar(self, p):
        target_px = 110.0
        meters = target_px / self.scale
        nice = 100
        for cand in (50, 100, 200, 250, 500, 1000, 2000, 5000):
            if not cand <= (meters * 1.6):
                continue
            nice = cand
        length = nice * self.scale
        x = ((self.width() - length) - 24)
        y = (self.height() - 26)
        p.setPen(QPen(QColor(255, 255, 255, 200), 2))
        p.drawLine(QPointF(x, y), QPointF((x + length), y))
        p.drawLine(QPointF(x, (y - 4)), QPointF(x, (y + 4)))
        p.drawLine(QPointF((x + length), (y - 4)), QPointF((x + length), (y + 4)))
        p.setPen(QColor(235, 240, 245))
        p.setFont(QFont('Microsoft YaHei UI', 9))
        label = f"{nice} m" if nice < 1000 else f"{(nice / 1000):g} km"
        p.drawText(QRectF(x, (y - 22), length, 18), Qt.AlignCenter, label)

    def set_timer_state(self, running):
        """「计时」按钮跟着计时状态走：计时中显示「结束」并高亮。"""
        running = bool(running)
        self.btn_timer.blockSignals(True)
        self.btn_timer.setChecked(running)
        self.btn_timer.setText('结束' if running else '计时')
        self.btn_timer.setToolTip('结束计时（用时已显示在浮窗第一行）' if running else '开始计时（00:00:00 往上走，不能暂停）；再点一次结束 —— 计时期间时间显示在浮窗第一行')
        self.btn_timer.blockSignals(False)

    def rel_of_flag(self, it):
        """旗点的世界坐标 -> 相对地图左上角的米数（w2s 吃的就是这一套）。"""
        if not self.map:
            return None
        return ((it['x'] - self.map.minx), (it['z'] - self.map.minz))

    def _flag_xy(self, it):
        r = self.rel_of_flag(it)
        if r is None:
            return None
        return self.w2s(r[0], r[1])

    def _flag_color(self, it, all_lanes, sel):
        if it.get('main'):
            return '#9fb6cf'
        if all_lanes and it.get('tags'):
            return flagzones.lane_color(it['tags'][0])
        if sel:
            return '#ffe066'
        return '#ffb347'

    def set_flags_on(self, on):
        """左上「旗点」按钮：开/关旗点与占领区，同时收放右上角面板。"""
        self.flags_on = bool(on)
        self.flag_sel = None
        if self.flags_on:
            self._reload_flags()
            self.panel.show()
            self._place_panel()
            self.panel.raise_()
            if self.map is None:
                self.flag_notice.emit('先把地图选上，再看旗点')
            elif not flagzones.has(self.map.slug):
                self.flag_notice.emit(f"「{self.map.label}」没有旗点数据（APK 里这张图也是空的）")
            else:
                info = (flagzones.layer_info(self.map.slug, self.flag_layer) or {})
                self.flag_notice.emit(f"旗点 · {(info.get('label') or self.flag_layer)} · 共 {len(self.flag_items)} 个（点旗点看占领区）")
        else:
            self.panel.hide()
            self.flag_notice.emit('已收起旗点')
        self.update()

    def _close_flag_panel(self):
        """面板右上角的 ×：连按钮一起复位（会走 set_flags_on(False)）。"""
        if self.btn_flags.isChecked():
            self.btn_flags.setChecked(False)
            return None
        self.set_flags_on(False)

    def _on_flag_opt(self, key, on):
        self._flag_lbl = {}
        self._flag_pm = None
        if key == 'zone':
            self.flag_show_zone = bool(on)
        elif key == 'cn':
            self.flag_show_cn = bool(on)
        elif key == 'en':
            self.flag_show_en = bool(on)
        elif key == 'seq':
            self.flag_show_seq = bool(on)
        self.update()

    def set_flag_layer(self, key):
        if not key or key == self.flag_layer:
            return None
        self.flag_layer = str(key)
        self._reload_flags()
        self.update()
        if self.flags_on and self.map is not None:
            info = (flagzones.layer_info(self.map.slug, self.flag_layer) or {})
            self.flag_notice.emit(f"旗点图层：{(info.get('label') or self.flag_layer)} · {len(self.flag_items)} 个")
            if not self.flag_items:
                self.flag_notice.emit('这个图层没有旗点（数据里是空的）')
                return None
            return None
        return None

    def set_flag_lane(self, lane):
        lane = str((lane or flagzones.ALL))
        if lane == self.flag_lane:
            return None
        self.flag_lane = lane
        self._reload_flags()
        self.update()
        if self.flags_on and self.map is not None:
            name = '全部旗点' if lane == flagzones.ALL else f"{lane} 链"
            self.flag_notice.emit(f"进攻链：{name} · {len(self.flag_items)} 个旗点")
            return None
        return None

    def _reload_flags(self):
        """地图 / 图层 / 进攻链变了：重算要画的旗点，并把面板填满。"""
        self.flag_items = []
        self.flag_sel = None
        self._flag_lbl = {}
        self._flag_gen = (getattr(self, '_flag_gen', 0) + 1)
        self._flag_pm = None
        m = self.map
        if m is None:
            if hasattr(self, 'panel'):
                self.panel.set_layers([])
                self.panel.set_lanes([])
                self.panel.set_lane_visible(False)
                self.panel.set_items([], False)
                self.panel.set_info('')
                self.panel.set_note('先在上面选一张地图')
            return None
        slug = m.slug
        if not flagzones.has(slug):
            if hasattr(self, 'panel'):
                self.panel.set_layers([])
                self.panel.set_lanes([])
                self.panel.set_lane_visible(False)
                self.panel.set_items([], False)
                self.panel.set_info('')
                self.panel.set_note('本图没有旗点数据')
            return None
        ls = flagzones.layers(slug)
        if self.flag_layer not in ls:
            self.flag_layer = ls[0]
        lanes = flagzones.lanes(slug, self.flag_layer)
        if lanes:
            if self.flag_lane not in ([flagzones.ALL] + lanes):
                self.flag_lane = flagzones.ALL
        else:
            self.flag_lane = flagzones.ALL
        self.flag_items = flagzones.items(slug, self.flag_layer, self.flag_lane)
        if not hasattr(self, 'panel'):
            return None
        self.panel.set_layers([(k, ((flagzones.layer_info(slug, k) or {}).get('label') or k)) for k in ls])
        if lanes:
            counts = flagzones.lane_counts(slug, self.flag_layer)
            info = (flagzones.layer_info(slug, self.flag_layer) or {})
            entries = [(flagzones.ALL, f"全部旗点（{len(lanes)} 条链 · {info.get('n', 0)} 个链位）")]
            entries += [(nm, f"{nm} 链（{counts.get(nm, 0)} 个节点）") for nm in lanes]
            self.panel.set_lanes(entries)
            self.panel.set_lane_visible(True)
        else:
            self.panel.set_lanes([])
            self.panel.set_lane_visible(False)
        self.panel.set_current(self.flag_layer, self.flag_lane)
        self.panel.set_items(self.flag_items, self.flag_lane == flagzones.ALL)
        self.panel.set_info('')
        self.panel.set_note(f"{len(self.flag_items)} 个旗点 · 点地图上的旗点或点这里一行查看")
        return None

    def select_flag(self, it, center=False):
        """选中一个旗点：地图上加亮、列表滚到那一行、面板与状态栏写详情。"""
        if not it or self.map is None:
            return None
        self.flag_sel = it.get('k')
        r = self.rel_of_flag(it)
        if center and r is not None:
            self.focus_point(r[0], r[1], scale=max(self.scale, 1.2))
        grid = ''
        try:
            if r is not None:
                grid = ('格号 ' + self.map.grid_at(r[0], r[1], 4))
        except Exception:
            grid = ''
        text = flagzones.info_text(it, self.flag_lane == flagzones.ALL, grid)
        if hasattr(self, 'panel'):
            self.panel.select_k(self.flag_sel)
            self.panel.set_info(text)
        self.flag_notice.emit(text)
        self.update()

    def focus_flag(self, k):
        """旗点列表点了一行：居中过去并选中。"""
        it = next((x for x in self.flag_items if x.get('k') == k), None)
        if it is not None:
            self.select_flag(it, center=True)
            return None

    def _clear_flag_sel(self):
        if self.flag_sel is not None:
            self.flag_sel = None
            if hasattr(self, 'panel'):
                self.panel.clear_sel()
            self.update()
            return None

    def _flag_hit(self, x, y):
        """点在旗点中心徽章（r=10）附近几像素内，返回那个旗点。

    APK 里命中半径是 46 px，但那边点地图不干别的事；这边点地图 = 放阵地，
    46 px 会把整片占领区都吃掉、阵地就放不下去了，所以这里只认徽章周围 16 px。
    想看某个旗点的详情还可以点右上角列表里的那一行。
    """
        best = None
        bd = 16.0
        for it in self.flag_items:
            p = self._flag_xy(it)
            if p is None:
                continue
            d = math.hypot((p[0] - x), (p[1] - y))
            if not d < bd:
                continue
            bd = d
            best = it
        return best

    def _draw_flag_zone(self, p, sh, sx, sy, main=False, sel=False):
        """画一块占领区：圆 / 矩形 / 胶囊，尺寸按当前缩放换算成像素。"""
        try:
            kind = int(sh[0])
        except Exception:
            return None
        except Exception:
            pass
        else:
            mpp = self.scale
            if sel:
                width = 2.6
                stroke = QColor(255, 240, 160, 242)
                fill = QColor(255, 240, 160, 51)
            elif main:
                width = 1.5
                stroke = QColor(170, 195, 225, 204)
                fill = QColor(150, 175, 205, 36)
            else:
                width = 1.5
                stroke = QColor(255, 190, 110, 184)
                fill = QColor(255, 170, 70, 28)
            p.save()
            p.translate(sx, sy)
            p.setPen(QPen(stroke, width, Qt.DashLine if (main and not sel) else Qt.SolidLine))
            p.setBrush(fill)
            if kind == 0:
                r = max(3.0, (float(sh[1]) * mpp))
                p.drawEllipse(QPointF(0, 0), r, r)
            elif kind == 1:
                w = max(3.0, (float(sh[1]) * mpp))
                h = max(3.0, (float(sh[2]) * mpp))
                if not sh[4]:
                    sh[4]
                if not sh[5]:
                    sh[5]
                p.translate((float(0) * mpp), (float(0) * mpp))
                if not sh[3]:
                    sh[3]
                p.rotate((-float(0)))
                p.drawRect(QRectF(((-w) / 2), ((-h) / 2), w, h))
            else:
                ln = max(3.0, (float(sh[1]) * mpp))
                rr = min(max(2.0, (float(sh[2]) * mpp)), (ln / 2))
                if not sh[4]:
                    sh[4]
                if not sh[5]:
                    sh[5]
                p.translate((float(0) * mpp), (float(0) * mpp))
                if not sh[3]:
                    sh[3]
                p.rotate((-float(0)))
                p.drawRoundedRect(QRectF((-rr), ((-ln) / 2), (2 * rr), ln), rr, rr)
        p.restore()

    def _flag_font(self, kind):
        """旗点用到的几种字体只建一次（一屏上百个旗点，每次重绘都 new QFont 太贵）。"""
        cache = getattr(self, '_flag_fonts', None)
        if cache is None:
            cache = self._flag_fonts = {}
        f = cache.get(kind)
        if f is None:
            f = QFont('Microsoft YaHei UI')
            if kind == 'cn':
                f.setPixelSize(12)
                f.setBold(True)
            elif kind == 'en':
                f.setPixelSize(10)
            elif kind == 'tags':
                f.setPixelSize(9)
            else:
                f.setPixelSize(10)
                f.setBold(True)
            cache[kind] = f
        return f

    def _flag_label(self, it):
        """算好一个旗点的名字标签，连底框和字一起烤成小图，每次重绘直接贴。

    一屏八十多个旗点，逐字 drawText 要 10 ms 上下；烤成 QPixmap 之后只剩 blit。
    """
        cache = getattr(self, '_flag_lbl', None)
        if cache is None:
            cache = self._flag_lbl = {}
        k = it.get('k')
        hit = cache.get(k)
        if hit is not None:
            return hit
        all_lanes = self.flag_lane == flagzones.ALL
        lines = []
        if self.flag_show_cn:
            lines.append((str((it.get('cn') or '')), 'cn', QColor('#ffe9a8')))
        if self.flag_show_en:
            lines.append((str((it.get('name') or '')), 'en', QColor('#cfd8e3')))
        if all_lanes and len((it.get('tags') or [])) > 1:
            lines.append(('·'.join(it['tags']), 'tags', QColor('#93a6bd')))
        if not lines:
            cache[k] = None
            return None
        texts = []
        mw = 0.0
        for txt, kind, col in lines:
            s = txt if len(txt) <= 20 else (txt[:19] + '…')
            texts.append((s, kind, col))
            mw = max(mw, QFontMetricsF(self._flag_font(kind)).horizontalAdvance(s))
        w = mw + 8.0
        h = (len(texts) * 13.0)
        dpr = max(1.0, float(self.devicePixelRatioF()))
        pm = QPixmap(max(1, int(math.ceil((w * dpr)))), max(1, int(math.ceil((h * dpr)))))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.transparent)
        q = QPainter(pm)
        q.setRenderHint(QPainter.Antialiasing, True)
        q.setPen(Qt.NoPen)
        q.setBrush(QColor(6, 10, 16, 184))
        q.drawRoundedRect(QRectF(0, 0, w, h), 3, 3)
        for j, (s, kind, col) in enumerate(texts):
            q.setFont(self._flag_font(kind))
            q.setPen(col)
            q.drawText(QPointF(4.0, (10.0 + (j * 13.0))), s)
        q.end()
        out = {'mw': mw, 'w': w, 'h': h, 'pm': pm}
        cache[k] = out
        return out

    def _flag_badge(self, tag, col):
        """序号徽章（圆底 + 字）也烤成小图，按「文字+颜色」缓存。"""
        cache = getattr(self, '_flag_badges', None)
        if cache is None:
            cache = self._flag_badges = {}
        key = (str(tag), col.rgb())
        hit = cache.get(key)
        if hit is not None:
            return hit
        dpr = max(1.0, float(self.devicePixelRatioF()))
        size = 24
        pm = QPixmap(int((size * dpr)), int((size * dpr)))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.transparent)
        q = QPainter(pm)
        q.setRenderHint(QPainter.Antialiasing, True)
        q.setPen(QPen(col, 2))
        q.setBrush(QColor(8, 12, 18, 235))
        q.drawEllipse(QPointF((size / 2), (size / 2)), 10, 10)
        q.setFont(self._flag_font('badge'))
        q.setPen(col)
        q.drawText(QRectF(1, 4, (size - 2), 16), Qt.AlignCenter, str(tag))
        q.end()
        cache[key] = pm
        return pm

    def _paint_flags(self, p):
        """旗点这一层先画到一张离屏图上缓存起来。

    一屏八十多个旗点（占领区 + 徽章 + 名字）要 25～30 ms，鼠标一动整块视口都要重绘，
    不加缓存拖动会发涩。变换（平移/缩放）、图层、开关、选中没变就直接贴上次那张。
    """
        key = (id(self.map), self.flag_layer, self.flag_lane, self.flag_show_zone, self.flag_show_cn, self.flag_show_en, self.flag_show_seq, self.flag_sel, round(self.scale, 6), round(self.ox, 3), round(self.oy, 3), self.width(), self.height(), getattr(self, '_flag_gen', 0))
        dpr = max(1.0, float(self.devicePixelRatioF()))
        want = (int((self.width() * dpr)), int((self.height() * dpr)))
        if want[0] <= 0 or want[1] <= 0:
            return
        pm = getattr(self, '_flag_pm', None)
        fresh = (pm is None) or pm.isNull() or pm.width() != want[0] or pm.height() != want[1]
        # 注意：这里必须是「缓存没命中就重建」，而不是「有缓存才重建」。
        # 早期版本把整段重建套在 if pm is not None: 里，首帧 _flag_pm 为 None
        # 时直接落到 drawPixmap(0, 0, None) → TypeError（迁移测试时触发的那次崩溃）。
        if fresh or getattr(self, '_flag_pm_key', None) != key:
            if fresh:
                pm = QPixmap(*want)
            pm.setDevicePixelRatio(dpr)
            pm.fill(Qt.transparent)
            q = QPainter(pm)
            try:
                self.draw_flags(q)
            finally:
                q.end()
            self._flag_pm = pm
            self._flag_pm_key = key
        if pm is not None and not pm.isNull():
            p.drawPixmap(0, 0, pm)

    def draw_flags(self, p):
        """把当前图层的旗点画在地图上：占领区 + 序号徽章 + 名字（跟 APK 的画法一致）。"""
        if not self.flag_items or self.map is None:
            return None
        all_lanes = self.flag_lane == flagzones.ALL
        boxes = []
        w, h = self.width(), self.height()
        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)
        for it in self.flag_items:
            pt = self._flag_xy(it)
            if pt is None:
                continue
            sx, sy = pt
            if not (-80 <= sx <= (w + 80) and -80 <= sy <= (h + 80)):
                continue
            sel = (self.flag_sel is not None and self.flag_sel == it.get('k'))
            col = QColor(self._flag_color(it, all_lanes, sel))
            if self.flag_show_zone and it.get('shapes'):
                for sh in it['shapes']:
                    self._draw_flag_zone(p, sh, sx, sy, bool(it.get('main')), sel)
                    continue
            if (self.flag_show_seq or it.get('main')):
                pm = self._flag_badge((it.get('tag') or ''), col)
                p.drawPixmap(QPointF((sx - 12), (sy - 12)), pm)
            if sel:
                p.setBrush(Qt.NoBrush)
                p.setPen(QPen(QColor(255, 224, 102, 190), 1.4, Qt.DashLine))
                p.drawEllipse(QPointF(sx, sy), 15, 15)
            lab = self._flag_label(it)
            if not lab:
                continue
            mw = lab['mw']
            rect = QRectF(((sx - (mw / 2)) - 4), (((sy - 25) - lab['h']) + 13), lab['w'], lab['h'])
            if any((rect.adjusted(-3, -1, 3, 1).intersects(b) for b in boxes)):
                continue
            boxes.append(rect)
            p.drawPixmap(QPointF(rect.left(), rect.top()), lab['pm'])
        p.restore()

    def wheelEvent(self, ev):
        if ev.angleDelta().y() > 0:
            self.zoom_at(ev.position().x(), ev.position().y(), 1.25)
            return None
        self.zoom_at(ev.position().x(), ev.position().y(), 0.8)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._press = ev.position()
            self._moved = False
            return None
        if ev.button() == Qt.RightButton and self.gun:
            self.gun = None
            self.update()
            self.gun_set.emit(-1.0, -1.0)
            return None
        return None

    def mouseMoveEvent(self, ev):
        if self.map:
            rx, rz = self.s2w(ev.position().x(), ev.position().y())
            new = (rx, rz) if 0 <= rx <= self.map.spanx and 0 <= rz <= self.map.spanz else None
            if self._hover_key(new) != self._hover_key(self.hover):
                self.hover = new
                self.update()
            else:
                self.hover = new
        if self._press is None or not self.map:
            return None
        pos = ev.position()
        dx = (pos.x() - self._press.x())
        dy = (pos.y() - self._press.y())
        if not self._moved and (abs(dx) + abs(dy)) > 4:
            self._moved = True
        if self._moved:
            self.ox += dx
            self.oy += dy
            self._press = pos
            self.update()
            return None

    def mouseReleaseEvent(self, ev):
        if (ev.button() != Qt.LeftButton or self._press is None):
            return None
        if not self._moved and self.map:
            mx, my = ev.position().x(), ev.position().y()
            if self.flags_on and self.flag_items:
                hit = self._flag_hit(mx, my)
                if hit is not None:
                    self.select_flag(hit)
                    self._press = None
                    return None
                self._clear_flag_sel()
            rx, rz = self.s2w(mx, my)
            if 0 <= rx <= self.map.spanx and 0 <= rz <= self.map.spanz:
                self.gun = (rx, rz)
                self.gun_set.emit(rx, rz)
                self.update()
        self._press = None

    def leaveEvent(self, _ev):
        if self.hover is not None:
            self.hover = None
            self.update()
            return None

    def showEvent(self, ev):
        super().showEvent(ev)

    def _place_btns(self):
        self.btn_locate.move(12, 12)
        self.btn_grid.move(82, 12)
        self.btn_flags.move(152, 12)
        self.btn_timer.move(222, 12)

    def _place_panel(self):
        if not hasattr(self, 'panel'):
            return None
        w = 268
        h = max(236, min(420, (self.height() - 100)))
        self.panel.setFixedSize(w, h)
        self.panel.move(max(12, ((self.width() - w) - 12)), 48)

    def mouseDoubleClickEvent(self, ev):
        """双击旗点：把地图居中过去（跟 APK 列表点行一个效果）。"""
        if ev.button() == Qt.LeftButton and self.flags_on and self.flag_items:
            hit = self._flag_hit(ev.position().x(), ev.position().y())
            if hit is not None:
                self.focus_flag(hit.get('k'))
                return None
        super().mouseDoubleClickEvent(ev)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if hasattr(self, 'btn_locate'):
            self._place_btns()
        if hasattr(self, 'panel'):
            self._place_panel()
        if not self._user_zoom:
            self.fit()
            return None

class MiniMap(QWidget):
    """「目标修正」小地图：以估测目标为心的正方形视野（望远镜 120 m / 无人机 60 m）。

    望远镜流程在距离超过 100 m 时弹一次；间瞄·无人机视角的快速定位读完目标坐标后直接弹
    （60 m）。两种都用方位角把「玩家的正前方」转到图的正上方。窗口中心一次性挪到鼠标位置
    （之后不跟随），玩家点一下目标的真实位置，就把点击处当成目标交给主窗口重算诸元；
    点击处停在原处 1 秒，然后整个窗口自动消失。点不到（误差太大）就由主窗口按 ⌫ 取消。
    """
    picked = Signal(float, float)
    closed = Signal()
    stage_pick = Signal(str, float, float)
    SIZE = 272
    PAD = 13
    BASE_SPAN = 100.0
    SPAN = 120.0
    PPM = ((SIZE - (2 * PAD)) / BASE_SPAN)
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags((((Qt.Tool | Qt.FramelessWindowHint) | Qt.WindowStaysOnTopHint) | Qt.WindowDoesNotAcceptFocus))
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setWindowTitle('目标修正')
        self.setCursor(Qt.CrossCursor)
        self.setFixedSize(self._side_for(self.SPAN), self._side_for(self.SPAN))
        self.map = None
        self.img = None
        self.span = self.SPAN
        self.center = (0.0, 0.0)
        self.est_grid = ''
        self.az = 0.0
        self.me = None
        self.mark = None
        self.hover = None
        self.spread = None
        self.allow_north = False
        self._up_down = False
        self._keys = QTimer(self)
        self._keys.setInterval(40)
        self._keys.timeout.connect(self._poll_up)
        self._close = QTimer(self)
        self._close.setSingleShot(True)
        self._close.timeout.connect(self.hide)
        self.stage = ''
        self.stage_start = (0.0, 0.0)
        self.stage_a = None
        self.stage_b = None
        self.stage_normal = None
        self.stage_depth = 0.0
        self.stage_msg = ''
        self.stage_title = ''
        self.stage_prompt = ''
        self.los = None

    @classmethod
    def _side_for(cls, span):
        """视野边长（米）→ 窗口边长（像素）。望远镜的放大倍率固定成 2.46 像素/米，
    所以 120 m 视野的窗口比 100 m 时大 1.2 倍（272 → 321），不是把 120 m 塞进
    272 像素里（那样等于把图缩小、倍率就变了）。无人机那条 60 m 仍用 272 的窗口。"""
        try:
            span = float(span)
        except (TypeError, ValueError):
            span = cls.SPAN
        if span <= (cls.BASE_SPAN + 0.5):
            return cls.SIZE
        # 弹道被挡时视野要同时装下目标和挡点（可能几百米），但窗口不能大到出屏：
        # PPM 是「不缩图」的倍率，到这里就封顶 420 px，超出的部分靠缩小倍率塞进去。
        return min((int(round((cls.PPM * span))) + (2 * cls.PAD)), 420)

    def _inner(self):
        return QRectF(self.PAD, self.PAD, (self.width() - (2 * self.PAD)), (self.height() - (2 * self.PAD)))

    def _pick_rect(self):
        """能点的范围：地图本体（去掉上下两条说明栏，免得点在被挡住的图上）。"""
        i = self._inner()
        return QRectF(i.left(), (i.top() + 21), i.width(), (i.height() - 42))

    def _s(self):
        """屏幕像素 / 米。"""
        return (self._inner().width() / max(1.0, self.span))

    def _grid_step(self):
        """画网格用的间距（米）：望远镜视野（100/120 m）对齐 4 级网格的第 3 级
    子格（33.3 m），60 m 视野太密反而看不清，用一个整的 20 m。"""
        if self.span >= 99.0:
            return core.KP_M / 9.0
        return self.span / 3.0

    def _bar_m(self):
        """比例尺长度（米）：不超过视野 1/4、凑成好看整数的那个（120 m→25 m，60 m→10 m）。"""
        want = self.span / 4.0
        for v in (250.0, 100.0, 50.0, 25.0, 10.0, 5.0):
            if not v <= (want + 1e-06):
                continue
            return v
        return 5.0

    def _rot(self):
        """把世界（东 x / 南 z）转到屏幕的旋转：正上方 = 玩家正前方（方位角 az）。"""
        a = math.radians((self.az or 0.0))
        return (math.cos(a), math.sin(a))

    def w2s(self, rx, rz):
        """世界坐标 → 屏幕像素（先按方位角旋转，再以中心缩放）。"""
        c = self._inner().center()
        s = self._s()
        ca, sa = self._rot()
        dx, dz = rx - self.center[0], rz - self.center[1]
        return QPointF((c.x() + (((ca * dx) + (sa * dz)) * s)), (c.y() + ((((-sa) * dx) + (ca * dz)) * s)))

    def s2w(self, sx, sy):
        """屏幕像素 → 世界坐标（w2s 的逆）。"""
        c = self._inner().center()
        s = self._s()
        ca, sa = self._rot()
        px, py = (sx - c.x()) / s, (sy - c.y()) / s
        return (((self.center[0] + (ca * px)) - (sa * py)), ((self.center[1] + (sa * px)) + (ca * py)))

    def _arrow(self):
        """绿三角：玩家的屏幕位置 + 朝中心（正前方）的单位方向。"""
        c = self._inner().center()
        sp = self.w2s(*self.me) if self.me is not None else QPointF(c.x(), (c.y() + 200.0))
        pr = self._pick_rect().adjusted(12, 12, -12, -34)
        ax = max(pr.left(), min(sp.x(), pr.right()))
        ay = max(pr.top(), min(sp.y(), pr.bottom()))
        dx, dy = c.x() - ax, c.y() - ay
        ln = math.hypot(dx, dy)
        if ln < 1e-06:
            return None
        return (ax, ay, (dx / ln), (dy / ln))

    def show_at(self, center, m, img, est_grid='', az=None, me=None, span=None, north_key=False, spread=None, los=None):
        """弹出小地图。center=估测目标，az=玩家看目标的方位角（图的正上方就是它），
    me=玩家自己的世界坐标（画绿三角，可 None），span=视野边长（米，默认 120），
    north_key=True 时，等玩家点图的这段时间里按一下方向键「上」就把正北转到上方
    （只有望远镜那两张图这样；无人机 60 m 那张不响应）。
    spread=当前解的散布椭圆（见 set_spread），None 就不画；
    los=弹道被挡的描述（见 set_los），None 就是不挡。"""
        if not m or img is None:
            return False
        self.map = m
        self.img = img
        try:
            self.span = float(span) if span else self.SPAN
        except (TypeError, ValueError):
            self.span = self.SPAN
        side = self._side_for(self.span)
        if (self.width() != side or self.height() != side):
            self.setFixedSize(side, side)
        self.center = (float(center[0]), float(center[1]))
        self.est_grid = str((est_grid or ''))
        self.az = 0.0 if az is None else float(az)
        self.me = None if me is None else (float(me[0]), float(me[1]))
        self.mark = None
        self.hover = None
        self.spread = spread
        self.los = None if self.stage else los
        self.allow_north = (bool(north_key) and self.span >= 99.0)
        self._up_down = self._up_held()
        self._keys.stop()
        self._close.stop()
        self._place_at_cursor()
        self.update()
        self.show()
        self.raise_()
        if self.allow_north:
            self._keys.start()
        return True

    def _place_at_cursor(self):
        """窗口中心一次性对齐鼠标位置（不是持续跟随），并夹在当前屏幕的可用区域里。"""
        pos = QCursor.pos()
        scr = (QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen())
        g = scr.availableGeometry() if scr is not None else QRect(0, 0, 1280, 720)
        x = (pos.x() - (self.width() // 2))
        y = (pos.y() - (self.height() // 2))
        x = max((g.left() + 4), min(x, ((g.right() - self.width()) - 3)))
        y = max((g.top() + 4), min(y, ((g.bottom() - self.height()) - 3)))
        self.move(x, y)

    @staticmethod
    def _up_held():
        """方向键「上」（VK_UP = 0x26，不是 W）现在是不是按着。

    用 GetAsyncKeyState 轮询而不是注册全局热键：不抢键，游戏照常收到这个方向键；
    也不需要焦点在本窗口（浮窗本来就不接受焦点）。"""
        try:
            return bool((ctypes.windll.user32.GetAsyncKeyState(38) & 32768))
        except Exception:
            return False

    def _poll_up(self):
        """等玩家点目标的这段时间盯住 ↑：按一下就把正北转到图的正上方（只转一次）。"""
        if (not self.isVisible() or self.mark is not None or not self.allow_north):
            self._keys.stop()
            return None
        down = self._up_held()
        if down and not self._up_down:
            self.set_north_up()
        self._up_down = down

    def set_north_up(self):
        """把图转成「正北朝上」（方位 az = 0）。已经是正北朝上就什么也不做。"""
        if abs(((self.az or 0.0) % 360.0)) < 0.5:
            return False
        self.az = 0.0
        self.hover = None
        self.update()
        try:
            print('[minimap] 已转成正北朝上', flush=True)
        except Exception:
            return True
        return True

    def set_spread(self, spread=None):
        """把「当前这一解的散布椭圆」交给小地图画（半透明亮红）。

    spread = {'c': 弹着点世界坐标, 'cross': 横向半轴（米，sprA）, 'along':
    纵向半轴（米，sprB）, 'ang': 纵向轴的世界方位（度，东 0、南 +90，和 w2s
    同一套坐标）}。None = 不画。只在「目标修正」这类普通弹图里画；呼叫弹幕
    分步选点（stage）有自己的图，传进来也不画。
    窗口没弹出来时只记住，不重画——这样 show_at 之后再 set 也不会闪一下。"""
        self.spread = None if self.stage else spread
        if self.isVisible():
            self.update()

    def set_los(self, los=None):
        """把「这条弹道被谁挡住」交给小地图（红色斜线 = 打不到的那片区域）。

    los = {'hit': 挡点世界坐标, 's': 挡点离炮位多少米, 'label': 地形/建筑……,
    'foot': 挡人地物的四角（可 None）, 'tgt': 目标世界坐标,
    'shadow': 被挡区域的四边形（可 None）, 'alt_ok': 换个角档能不能越过}。
    None = 通畅。只在普通「目标修正」图里画；呼叫弹幕分步选点（stage）不画。"""
        self.los = None if self.stage else los
        if self.isVisible():
            self.update()

    def _paint_spread(self, p, inner):
        """半透明亮红椭圆：横向半轴 = 角散布在弹着点横向拉开的距离（sprA），
    纵向半轴 = 高/低角差拉开的距离（sprB），长轴顺着炮位→目标连线。

    跟 APK（Yehorivka 0.50.0「间瞄武器计算」）同一个口径：那边是
    ctx.ellipse(cx, cy, sprA, sprB, 方位 + 90°)，即 sprA 垂直于连线、
    sprB 顺着连线，这里只是把它填成半透明色并加一圈亮红描边。
    散布比视野还大时就照实画（整块视野都会被染红），不缩放——缩了会骗人。"""
        sp = self.spread or {}
        try:
            tx, tz = (float(sp['c'][0]), float(sp['c'][1]))
            wd = max(0.5, float(sp['cross']))
            lg = max(0.5, float(sp['along']))
            ang = float(sp['ang'])
        except (KeyError, TypeError, ValueError, IndexError):
            return None
        s = self._s()
        if not math.isfinite(s) or s <= 0:
            return None
        cp = self.w2s(tx, tz)
        p.save()
        p.setClipRect(inner)
        p.translate(cp)
        p.rotate(ang - (self.az or 0.0))
        # 散布比视野大时整块图都会被染红：把填充调淡一点，底图还看得清
        big = (max(lg, wd) * s) > (0.35 * inner.width())
        p.setPen(QPen(QColor(255, 96, 96, 240), 2.0))
        p.setBrush(QColor(255, 66, 72, 40 if big else 58))
        p.drawEllipse(QPointF(0.0, 0.0), (lg * s), (wd * s))
        p.restore()

    def _paint_los(self, p, inner):
        """弹道被挡：挡点之后打不到的那片区域画红色斜线，挡点和挡住它的地物画出来。

        斜线区（shadow）是从挡点往后拉开的扇面，跟「这一发在这个方向上根本过不去」
        是一回事：它盖住的不是「修正过的目标区域」，而是修正也救不回来的地方。
        挡点跑到视野外时贴着边框给虚线箭头 + 距离，别让玩家以为图上是全部。"""
        d = self.los or {}
        try:
            hx, hz = float(d["hit"][0]), float(d["hit"][1])
            tx, tz = float(d["tgt"][0]), float(d["tgt"][1])
            s_hit = float(d.get("s") or 0.0)
        except (KeyError, TypeError, ValueError, IndexError):
            return None
        p.save()
        p.setClipRect(inner)
        sh = d.get("shadow") or []
        if len(sh) >= 3:
            try:
                poly = [self.w2s(float(q[0]), float(q[1])) for q in sh]
            except (TypeError, ValueError, IndexError):
                poly = []
            if len(poly) >= 3:
                # 先铺一层很淡的红把这些区域标出来，再叠斜线：斜线是国际象棋棋盘式的
                # 「打不到」记号，单靠斜线在偏棕的地图上太细、看不清。
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(255, 66, 72, 58))
                p.drawPolygon(poly)
                p.setPen(QPen(QColor(255, 96, 96, 190), 1.0))
                p.setBrush(QBrush(QColor(255, 168, 168, 205), Qt.BDiagPattern))
                p.drawPolygon(poly)
        ft = d.get("foot")
        if ft:
            try:
                poly = [self.w2s(float(q[0]), float(q[1])) for q in ft]
            except (TypeError, ValueError, IndexError):
                poly = []
            if len(poly) >= 3:
                p.setBrush(Qt.NoBrush)
                p.setPen(QPen(QColor(255, 132, 96, 235), 2.0))
                p.drawPolygon(poly)
        tp = self.w2s(tx, tz)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(255, 96, 96, 235), 2.0))
        p.drawEllipse(tp, 7, 7)
        hp = self.w2s(hx, hz)
        inside = inner.contains(hp)
        if inside:
            p.setPen(QPen(QColor(255, 236, 120, 245), 2.4))
            r = 6.0
            p.drawLine(QPointF((hp.x() - r), (hp.y() - r)), QPointF((hp.x() + r), (hp.y() + r)))
            p.drawLine(QPointF((hp.x() - r), (hp.y() + r)), QPointF((hp.x() + r), (hp.y() - r)))
        p.restore()
        room = inner.adjusted(6, 26, -6, -26)
        ax = max(room.left(), min(hp.x(), room.right()))
        ay = max(room.top(), min(hp.y(), room.bottom()))
        if not inside:
            cc = self._inner().center()
            p.setPen(QPen(QColor(255, 96, 96, 150), 1, Qt.DashLine))
            p.drawLine(cc, QPointF(ax, ay))
            dxa, dya = (ax - cc.x()), (ay - cc.y())
            ln = math.hypot(dxa, dya)
            if ln > 1e-06:
                ux, uy = (dxa / ln), (dya / ln)
                n = 6.0
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(255, 96, 96, 230))
                p.drawPolygon([QPointF((ax + (ux * n)), (ay + (uy * n))),
                               QPointF(((ax - ((ux * n) * 0.6)) + ((uy * n) * 0.8)), ((ay - ((uy * n) * 0.6)) - ((ux * n) * 0.8))),
                               QPointF(((ax - ((ux * n) * 0.6)) - ((uy * n) * 0.8)), ((ay - ((uy * n) * 0.6)) + ((ux * n) * 0.8)))])
        label = str(d.get('label') or '障碍')
        txt = f"挡 {s_hit:.0f} m · {label}"
        f = QFont('Microsoft YaHei UI', 9)
        f.setBold(True)
        p.setFont(f)
        tw = QFontMetrics(f).horizontalAdvance(txt)
        bx = (ax + 10)
        if ((bx + tw) > (inner.right() - 4)):
            bx = (ax - 10 - tw)
        by = (ay - 10)
        if (by - 11) < (inner.top() + 24):
            by = (ay + 20)
        bx = max((inner.left() + 4), bx)
        p.fillRect(QRectF((bx - 3), (by - 12), (tw + 6), 16), QColor(9, 13, 19, 190))
        p.setPen(QColor(255, 226, 130))
        p.drawText(QPointF(bx, by), txt)

    def _clamp_geo(self):
        scr = QGuiApplication.screenAt(self.frameGeometry().center())
        if scr is None:
            scr = QGuiApplication.primaryScreen()
        if scr is None:
            return None
        av = scr.availableGeometry()
        w, h = self.width(), self.height()
        x = min(max(self.x(), (av.left() + 4)), max((av.left() + 4), ((av.right() - w) - 3)))
        y = min(max(self.y(), (av.top() + 4)), max((av.top() + 4), ((av.bottom() - h) - 3)))
        self.move(x, y)

    def stage_width(self):
        if not self.stage_a or not self.stage_b:
            return 0.0
        return math.hypot((self.stage_b[0] - self.stage_a[0]), (self.stage_b[1] - self.stage_a[1]))

    def begin_stage(self, kind, start, m, img, span, title='', prompt='', az=None, north_key=False):
        """呼叫弹幕第一步：以起始点为心弹图，等玩家点宽度。
    下一步用 set_stage 换 —— 窗口不关、不挪，只有画的东西和说明变。

    az=图的正上方要对准哪个方位角（None=正北朝上）；north_key=True 时，等玩家点的
    这段时间里按一下方向键「上」可以把正北转到上方（只有望远镜视角这样传）。"""
        if not m or img is None:
            return False
        self.map = m
        self.img = img
        self.span = max(20.0, float(span))
        side = self._side_for(self.span)
        if self.width() != side:
            c = self.geometry().center()
            self.setFixedSize(side, side)
            self.move((c.x() - (side // 2)), (c.y() - (side // 2)))
            self._clamp_geo()
        was_stage = bool(self.stage)
        if az is not None:
            try:
                self.az = float(az) % 360.0
            except (TypeError, ValueError):
                self.az = 0.0
        elif not was_stage:
            self.az = 0.0
        self.center = (float(start[0]), float(start[1]))
        self.est_grid = ''
        self.me = None
        self.mark = None
        self.hover = None
        self.allow_north = bool(north_key) and self.span >= 99.0
        self._up_down = self._up_held()
        self._keys.stop()
        self._close.stop()
        self.stage = kind
        self.stage_start = (float(start[0]), float(start[1]))
        self.stage_a = None
        self.stage_b = None
        self.stage_normal = None
        self.stage_depth = 0.0
        self.stage_msg = ''
        self.stage_title = title
        self.stage_prompt = prompt
        self.spread = None
        if not self.isVisible():
            self._place_at_cursor()
            self.show()
        self.raise_()
        self.update()
        if self.allow_north:
            self._keys.start()
        return True

    def set_stage(self, kind, title='', prompt='', a=None, b=None, normal=None, depth=0.0, msg=''):
        """换到下一步（宽度 → 方向）：图不关，只更新要画/要提示的内容。"""
        self.stage = kind
        if title:
            self.stage_title = title
        if prompt:
            self.stage_prompt = prompt
        self.stage_msg = msg
        if a is not None:
            self.stage_a = (float(a[0]), float(a[1]))
        if b is not None:
            self.stage_b = (float(b[0]), float(b[1]))
        if normal is not None:
            self.stage_normal = (float(normal[0]), float(normal[1]))
        if depth:
            self.stage_depth = float(depth)
        self.mark = None
        self.hover = None
        self._close.stop()
        self.update()

    def end_stage(self):
        """分步点选结束（图已收起）：清干净，别影响下一次普通修正。"""
        self._keys.stop()
        self.allow_north = False
        self._up_down = False
        self.stage = ''
        self.stage_a = None
        self.stage_b = None
        self.stage_normal = None
        self.stage_depth = 0.0
        self.stage_msg = ''
        self.stage_title = ''
        self.stage_prompt = ''

    def mousePressEvent(self, ev):
        if ev.button() == Qt.RightButton:
            self.hide()
            return None
        if ev.button() != Qt.LeftButton:
            return None
        if self.stage:
            pos = ev.position()
            if not self._pick_rect().contains(pos):
                return None
            rx, rz = self.s2w(pos.x(), pos.y())
            rx = max(0.0, min(rx, self.map.spanx))
            rz = max(0.0, min(rz, self.map.spanz))
            self.hover = None
            self.update()
            self.stage_pick.emit(self.stage, rx, rz)
            return None
        if self.mark is not None:
            return None
        pos = ev.position()
        if not self._pick_rect().contains(pos):
            return None
        rx, rz = self.s2w(pos.x(), pos.y())
        rx = max(0.0, min(rx, self.map.spanx))
        rz = max(0.0, min(rz, self.map.spanz))
        self.mark = (rx, rz)
        self.hover = None
        self._keys.stop()
        self.update()
        self._close.start(1000)
        self.picked.emit(rx, rz)

    def mouseMoveEvent(self, ev):
        if (self.mark is not None or not self._pick_rect().contains(ev.position())):
            return None
        self.hover = self.s2w(ev.position().x(), ev.position().y())
        self.update()

    def leaveEvent(self, _ev):
        if self.hover is not None:
            self.hover = None
            self.update()
            return None

    def hideEvent(self, ev):
        self._close.stop()
        self._keys.stop()
        self.mark = None
        self.hover = None
        self._up_down = False
        super().hideEvent(ev)
        self.closed.emit()
        self.end_stage()

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        outer = QRectF(1, 1, (self.width() - 2), (self.height() - 2))
        frame = QPainterPath()
        frame.addRoundedRect(outer, 10, 10)
        p.fillPath(frame, QColor(9, 13, 19, 236))
        inner = self._inner()
        if not self.map or self.img is None:
            p.setPen(QColor(160, 178, 196))
            p.setFont(QFont('Microsoft YaHei UI', 10))
            p.drawText(self.rect(), Qt.AlignCenter, '还没有地图')
            p.setPen(QPen(QColor(255, 178, 90, 200), 2))
            p.drawPath(frame)
            return None
        s = self._s()
        cx, cz = self.center
        half = self.span / 2.0
        ca, sa = self._rot()
        ext = ((half * (abs(ca) + abs(sa))) + 1.0)
        x0, x1 = max(0.0, (cx - ext)), min(self.map.spanx, (cx + ext))
        z0, z1 = max(0.0, (cz - ext)), min(self.map.spanz, (cz + ext))
        if x1 > x0 and z1 > z0:
            iw, ih = self.img.width(), self.img.height()
            src = QRectF(((x0 / self.map.spanx) * iw), ((z0 / self.map.spanz) * ih), (((x1 - x0) / self.map.spanx) * iw), (((z1 - z0) / self.map.spanz) * ih))
            p.save()
            p.setClipRect(inner)
            p.translate(inner.center())
            p.rotate((-(self.az or 0.0)))
            p.scale(s, s)
            p.translate((-cx), (-cz))
            p.drawImage(QRectF(x0, z0, (x1 - x0), (z1 - z0)), self.img, src)
            p.restore()
        step = self._grid_step()
        p.save()
        p.setClipRect(inner)
        p.setPen(QPen(QColor(200, 220, 240, 38), 1))
        k = (int(math.floor(((cx - ext) / step))) + 1)
        while (k * step) < (cx + ext):
            p.drawLine(self.w2s((k * step), (cz - ext)), self.w2s((k * step), (cz + ext)))
            k += 1
        k = (int(math.floor(((cz - ext) / step))) + 1)
        while (k * step) < (cz + ext):
            p.drawLine(self.w2s((cx - ext), (k * step)), self.w2s((cx + ext), (k * step)))
            k += 1
        p.restore()
        if self.spread and (not self.stage):
            self._paint_spread(p, inner)
        if self.los and (not self.stage):
            self._paint_los(p, inner)
        if self.stage:
            self._paint_stage(p, inner)
        cc = self.w2s(cx, cz)
        p.setPen(QPen(QColor(90, 220, 255, 130), 1, Qt.DashLine))
        p.drawLine(QPointF(cc.x(), inner.top()), QPointF(cc.x(), inner.bottom()))
        p.drawLine(QPointF(inner.left(), cc.y()), QPointF(inner.right(), cc.y()))
        p.setPen(QPen(QColor(120, 230, 255, 235), 2))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(cc, 5, 5)
        if self.hover is not None and self.mark is None:
            hv = self.w2s(*self.hover)
            p.setPen(QPen(QColor(255, 255, 255, 110), 1, Qt.DotLine))
            p.drawLine(QPointF(hv.x(), inner.top()), QPointF(hv.x(), inner.bottom()))
            p.drawLine(QPointF(inner.left(), hv.y()), QPointF(inner.right(), hv.y()))
        ap = self._arrow()
        if ap is not None:
            ax, ay, ux, uy = ap
            n = 7.0
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(126, 227, 139, 235))
            p.drawPolygon([QPointF((ax + (ux * n)), (ay + (uy * n))), QPointF(((ax - ((ux * n) * 0.55)) + ((uy * n) * 0.75)), ((ay - ((uy * n) * 0.55)) - ((ux * n) * 0.75))), QPointF(((ax - ((ux * n) * 0.55)) - ((uy * n) * 0.75)), ((ay - ((uy * n) * 0.55)) + ((ux * n) * 0.75)))])
        if self.mark is not None:
            mp = self.w2s(*self.mark)
            p.setPen(QPen(QColor(255, 209, 102, 160), 1, Qt.DotLine))
            p.drawLine(cc, mp)
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(255, 150, 90, 255), 2))
            p.drawEllipse(mp, 8, 8)
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                p.drawLine(QPointF((mp.x() + (dx * 3)), (mp.y() + (dy * 3))), QPointF((mp.x() + (dx * 13)), (mp.y() + (dy * 13))))
                continue
            off = math.hypot((self.mark[0] - cx), (self.mark[1] - cz))
            txt = f"偏 {off:.0f} m"
            tf = QFont('Consolas', 10)
            tf.setBold(True)
            p.setFont(tf)
            p.setPen(QColor(255, 209, 102))
            tw = QFontMetrics(tf).horizontalAdvance(txt)
            tx = (mp.x() + 14) if ((mp.x() + 14) + tw) < (inner.right() - 4) else ((mp.x() - 14) - tw)
            ty = (mp.y() - 12) if (mp.y() - 18) > (inner.top() + 22) else (mp.y() + 26)
            ty = max((inner.top() + 34), min(ty, (inner.bottom() - 26)))
            p.drawText(QPointF(tx, ty), txt)
        bar_m = self._bar_m()
        bar_y = (inner.bottom() - 30.0)
        p.setPen(QPen(QColor(255, 255, 255, 190), 2))
        p.drawLine(QPointF((inner.left() + 10), bar_y), QPointF(((inner.left() + 10) + (bar_m * s)), bar_y))
        p.setPen(QColor(255, 255, 255, 190))
        p.setFont(QFont('Consolas', 9))
        p.drawText(QPointF((inner.left() + 10), (bar_y - 4)), f"{bar_m:g} m")
        np_ = self.w2s(cx, (cz - (0.46 * self.span)))
        pick = self._pick_rect()
        if self._inner().contains(np_):
            np_ = QPointF(max((pick.left() + 10.0), min(np_.x(), (pick.right() - 10.0))), max(np_.y(), (pick.top() + 12.0)))
            nf = QFont('Consolas', 9)
            nf.setBold(True)
            p.setFont(nf)
            p.setPen(QColor(150, 200, 255, 165))
            p.drawText(QRectF((np_.x() - 9), (np_.y() - 9), 18, 18), Qt.AlignCenter, 'N')
        def bar(rect, text, color, bold=False):
            p.fillRect(rect, QColor(9, 13, 19, 200))
            f = QFont('Microsoft YaHei UI', 9)
            f.setBold(bold)
            p.setFont(f)
            p.setPen(color)
            shown = QFontMetrics(f).elidedText(text, Qt.ElideRight, int((rect.width() - 8)))
            p.drawText(rect, Qt.AlignCenter, shown)

        if self.stage:
            head = self.stage_title or '呼叫弹幕'
        else:
            head = f"目标修正 · 估 {self.est_grid}" if self.est_grid else f"目标修正 · {self.span:g} × {self.span:g} m"
            _sp = self.spread or {}
            if _sp.get('cross') and _sp.get('along'):
                head += f" · 散布 {float(_sp['cross']):.0f}×{float(_sp['along']):.0f} m"
            if self.los:
                head += ' · 弹道被挡'
        bar(QRectF(inner.left(), inner.top(), inner.width(), 21), head, QColor(255, 209, 102), True)
        if self.stage:
            body = self.stage_prompt or '点一下'
            if self.stage_msg:
                tip = self.stage_msg
            else:
                up = ((self.az or 0.0) % 360.0)
                tip = ('上方=正北 · ' + body) if abs(up) < 0.5 else (('上方=前方 %.0f° · ' % up) + body)
                if self.allow_north:
                    fm9 = QFontMetrics(QFont('Microsoft YaHei UI', 9))
                    room = (inner.width() - 8.0)
                    if fm9.horizontalAdvance((tip + ' · ↑=正北')) <= room:
                        tip += ' · ↑=正北'
                    elif fm9.horizontalAdvance(('↑=正北 · ' + body)) <= room:
                        tip = '↑=正北 · ' + body
            bar(QRectF(inner.left(), (inner.bottom() - 21), inner.width(), 21), tip, QColor(255, 150, 90) if self.stage_msg else QColor(255, 209, 102), True)
        elif self.mark is not None:
            bar(QRectF(inner.left(), (inner.bottom() - 21), inner.width(), 21), '已标记 · 1 秒后关闭', QColor(255, 150, 90), True)
        else:
            up = ((self.az or 0.0) % 360.0)
            if self.los:
                tip = '斜线=打不到 · 点目标位置'
            else:
                tip = '上方=正北 · 点目标位置' if abs(up) < 0.5 else f"上方=你的正前方 {up:.0f}° · 点目标位置"
            if self.allow_north:
                fm = QFontMetrics(QFont('Microsoft YaHei UI', 9))
                room = (inner.width() - 8.0)
                if fm.horizontalAdvance((tip + ' · ↑=正北')) <= room:
                    tip += ' · ↑=正北'
                else:
                    tip = f"正前方 {up:.0f}° · ↑=正北 · 点目标位置"
            bar(QRectF(inner.left(), (inner.bottom() - 21), inner.width(), 21), tip, QColor(200, 220, 240))
        p.setPen(QPen(QColor(255, 178, 90, 210), 2))
        p.setBrush(Qt.NoBrush)
        p.drawPath(frame)

    def _paint_stage(self, p, inner):
        """呼叫弹幕：起始点十字、起始线（宽度）、推进矩形（方向 + 距离）。"""
        if not self.stage:
            return None
        p.save()
        p.setClipRect(inner)
        sp = self.w2s(*self.stage_start)
        if self.stage == 'start' and self.hover is not None:
            hv = self.w2s(*self.hover)
            p.setPen(QPen(QColor(255, 150, 90, 160), 1, Qt.DashLine))
            p.drawLine(sp, hv)
            d = math.hypot((self.hover[0] - self.stage_start[0]), (self.hover[1] - self.stage_start[1]))
            hf = QFont('Consolas', 10)
            hf.setBold(True)
            p.setFont(hf)
            p.setPen(QColor(255, 190, 120))
            txt = '偏 %.1f m' % d
            pick = self._pick_rect()
            fm = QFontMetrics(hf)
            tw = fm.horizontalAdvance(txt)
            th = fm.height()
            tx = (hv.x() + 9.0)
            if (tx + tw) > (pick.right() - 3.0):
                tx = ((hv.x() - 9.0) - tw)
            tx = max((pick.left() + 3.0), min(tx, ((pick.right() - tw) - 3.0)))
            ty = max((pick.top() + th), min((hv.y() - 6.0), (pick.bottom() - 3.0)))
            p.drawText(QPointF(tx, ty), txt)
        if self.stage == 'width' and self.hover is not None:
            hv = self.w2s(*self.hover)
            mp = self.w2s(((2.0 * self.stage_start[0]) - self.hover[0]), ((2.0 * self.stage_start[1]) - self.hover[1]))
            p.setPen(QPen(QColor(255, 209, 102, 150), 1, Qt.DashLine))
            p.drawLine(sp, hv)
            p.setPen(QPen(QColor(120, 230, 255, 130), 2, Qt.DashLine))
            p.drawLine(mp, hv)
        if self.stage_a and self.stage_b:
            a = self.w2s(*self.stage_a)
            b = self.w2s(*self.stage_b)
            p.setPen(QPen(QColor(120, 230, 255, 235), 3))
            p.drawLine(a, b)
            p.setBrush(QColor(120, 230, 255, 235))
            p.setPen(Qt.NoPen)
            p.drawEllipse(a, 3.5, 3.5)
            p.drawEllipse(b, 3.5, 3.5)
            wf = QFont('Consolas', 10)
            wf.setBold(True)
            p.setFont(wf)
            p.setPen(QColor(120, 230, 255))
            txt = f"宽 {self.stage_width():.0f} m"
            pick = self._pick_rect()
            fm = QFontMetrics(wf)
            tw = fm.horizontalAdvance(txt)
            th = fm.height()
            e = a if a.x() >= b.x() else b
            tx = (e.x() + 10.0)
            if (tx + tw) > (pick.right() - 3.0):
                tx = ((e.x() - 10.0) - tw)
            tx = max((pick.left() + 3.0), min(tx, ((pick.right() - tw) - 3.0)))
            ty = max((pick.top() + th), min((e.y() + (th * 0.35)), (pick.bottom() - 3.0)))
            p.drawText(QPointF(tx, ty), txt)
        if self.stage_normal and self.stage_depth > 0 and self.stage_a and self.stage_b:
            a = self.w2s(*self.stage_a)
            b = self.w2s(*self.stage_b)
            nx, nz = self.stage_normal
            far_a = self.w2s((self.stage_a[0] + (nx * self.stage_depth)), (self.stage_a[1] + (nz * self.stage_depth)))
            far_b = self.w2s((self.stage_b[0] + (nx * self.stage_depth)), (self.stage_b[1] + (nz * self.stage_depth)))
            poly = QPainterPath()
            poly.moveTo(a)
            poly.lineTo(b)
            poly.lineTo(far_b)
            poly.lineTo(far_a)
            poly.closeSubpath()
            p.setBrush(QColor(255, 150, 90, 46))
            p.setPen(QPen(QColor(255, 150, 90, 220), 2, Qt.DashLine))
            p.drawPath(poly)
            m0 = QPointF(((a.x() + b.x()) / 2.0), ((a.y() + b.y()) / 2.0))
            m1 = self.w2s((self.stage_start[0] + (nx * self.stage_depth)), (self.stage_start[1] + (nz * self.stage_depth)))
            p.setPen(QPen(QColor(255, 209, 102, 235), 2))
            p.drawLine(m0, m1)
            df = QFont('Consolas', 10)
            df.setBold(True)
            p.setFont(df)
            p.setPen(QColor(255, 209, 102))
            txt = f"推进 {self.stage_depth:.0f} m"
            pick = self._pick_rect()
            fm = QFontMetrics(df)
            tw = fm.horizontalAdvance(txt)
            th = fm.height()
            tx = (m1.x() + 9.0)
            if (tx + tw) > (pick.right() - 3.0):
                tx = ((m1.x() - 9.0) - tw)
            tx = max((pick.left() + 3.0), min(tx, ((pick.right() - tw) - 3.0)))
            ty = (m1.y() + (th * 0.35))
            if (ty - th) < pick.top():
                ty = (m1.y() + (th * 1.05))
            ty = max((pick.top() + th), min(ty, (pick.bottom() - 4.0)))
            p.drawText(QPointF(tx, ty), txt)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, 220), 2))
        p.drawEllipse(sp, 5, 5)
        p.drawLine(QPointF((sp.x() - 10), sp.y()), QPointF((sp.x() + 10), sp.y()))
        p.drawLine(QPointF(sp.x(), (sp.y() - 10)), QPointF(sp.x(), (sp.y() + 10)))
        sf = QFont('Microsoft YaHei UI', 9)
        p.setFont(sf)
        p.setPen(QColor(255, 255, 255, 210))
        p.drawText(QPointF((sp.x() + 10), (sp.y() + 18)), '起始点')
        p.restore()

class RegionSelector(QWidget):
    picked = Signal(list)
    cancelled = Signal()
    def __init__(self, which='grid'):
        super().__init__()
        self._which = which
        self._done = False
        self.setWindowFlags(((Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint) | Qt.Tool))
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setCursor(Qt.CrossCursor)
        geo = QRect()
        for s in QGuiApplication.screens():
            geo = geo.united(s.geometry())
        self._geo = geo
        self.setGeometry(geo)
        self._start = None
        self._cur = None

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 90))
        p.setPen(QColor(255, 255, 255))
        p.setFont(QFont('Microsoft YaHei UI', 12))
        if self._which in ('az', 's_az'):
            hint = '拖拽框选望远镜要读的方位角数字（只框那几个数字）· 右键取消'
        elif self._which == 'r_az':
            hint = '拖拽框选侦察用的方位角数字（只框那几个数字）· 右键取消'
        else:
            hint = '拖拽框选 CURRENT GRID /「玩家位置：」那一小块 · 右键取消' if self._which in ('r_grid', 's_grid') else '拖拽框选无人机视角的 CURRENT GRID（只框坐标文字）· 右键取消'
        p.drawText(QRect(0, 24, self.width(), 30), Qt.AlignCenter, hint)
        if self._start:
            if self._cur:
                r = QRectF(QPointF(self._start), QPointF(self._cur)).normalized()
                p.setCompositionMode(QPainter.CompositionMode_Clear)
                p.fillRect(r, Qt.transparent)
                p.setCompositionMode(QPainter.CompositionMode_SourceOver)
                p.setPen(QPen(QColor(255, 178, 90), 2))
                p.drawRect(r)
                p.setPen(QColor(255, 209, 102))
                p.drawText(r.adjusted(4, -22, 0, 0), Qt.AlignLeft, f"{int(r.width())} × {int(r.height())}")
                return None
            return None

    def _cancel(self):
        if not self._done:
            self._done = True
            self.cancelled.emit()
        self.close()

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key_Escape:
            self._cancel()
            return None
        super().keyPressEvent(ev)

    def closeEvent(self, ev):
        if not self._done:
            self._done = True
            self.cancelled.emit()
        super().closeEvent(ev)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.RightButton:
            self._cancel()
            return None
        self._start = ev.position().toPoint()
        self._cur = self._start
        self.update()

    def mouseMoveEvent(self, ev):
        if self._start:
            self._cur = ev.position().toPoint()
            self.update()
            return None

    def mouseReleaseEvent(self, ev):
        if not self._start or not self._cur:
            return None
        r = QRect(QPoint(self._start), QPoint(self._cur)).normalized()
        if (r.width() < 8 or r.height() < 8):
            self._cancel()
            return None
        self._done = True
        self.close()
        dpr = (QGuiApplication.screenAt(r.center()) or QGuiApplication.primaryScreen()).devicePixelRatio()
        gx, gy = r.x() + self._geo.x(), r.y() + self._geo.y()
        self.picked.emit([round((gx * dpr)), round((gy * dpr)), round((r.width() * dpr)), round((r.height() * dpr))])

class DrillWindow(QWidget):
    """把一块假的 CURRENT GRID 摆进识别区域，用来端到端自测（不需要开游戏）。"""
    def __init__(self, region_phys, text, dpr, kind='grid'):
        super().__init__()
        self.setWindowFlags(((Qt.Tool | Qt.FramelessWindowHint) | Qt.WindowStaysOnTopHint))
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        x, y, w, h = region_phys
        self.setGeometry(round((x / dpr)), round((y / dpr)), max(60, round((w / dpr))), max(40, round((h / dpr))))
        self.text = text
        self.kind = kind
        self._bg = self._make_bg()

    def _make_bg(self):
        pm = QPixmap(self.width(), self.height())
        p = QPainter(pm)
        for i in range(0, self.height(), 14):
            c = QColor(96, 104, 66) if ((i // 14) % 2) else QColor(112, 118, 78)
            p.fillRect(0, i, self.width(), 14, c)
        p.end()
        return pm

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.drawPixmap(0, 0, self._bg)
        if self.kind == 'az':
            f = QFont('Arial', max(12, int((self.height() * 0.46))))
            f.setBold(True)
            p.setFont(f)
            p.setPen(QColor(255, 255, 255))
            p.drawText(self.rect(), Qt.AlignCenter, self.text)
            return None
        f = QFont('Arial', max(9, int((self.height() * 0.16))))
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(0, 0, 0))
        p.drawText(self.rect().adjusted(3, 0, 3, 0), (Qt.AlignHCenter | Qt.AlignTop), 'CURRENT GRID')
        f2 = QFont('Arial', max(10, int((self.height() * 0.2))))
        f2.setBold(True)
        p.setFont(f2)
        p.drawText(self.rect().adjusted(3, 0, 3, 0), Qt.AlignCenter, self.text)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._bg = self._make_bg()

class Hotkeys(QThread):
    """四个可自定义热键：1=快速定位 2=准确定位 3=清除坐标 4=浮窗显隐（都支持 Ctrl/Shift/Alt/Win 组合）。"""
    pressed = Signal(int)
    failed = Signal(int, int, int)
    def __init__(self, bindings, parent=None):
        super().__init__(parent)
        self._tid = 0
        self._stop = False
        self.bindings = [tuple(b) for b in bindings]

    def run(self):
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        self._tid = kernel32.GetCurrentThreadId()
        bindings = list(self.bindings)
        for hid, mods, vk in bindings:
            if user32.RegisterHotKey(None, hid, mods, vk):
                continue
            err = kernel32.GetLastError()
            print(f"[hotkey] id{hid} mods={mods:#x} vk={vk:#x} 注册失败（可能被别的程序占用），错误码 {err}", flush=True)
            self.failed.emit(hid, err, mods)
        msg = ctypes.wintypes.MSG() if hasattr(ctypes, 'wintypes') else None
        if msg is None:
            import ctypes.wintypes as wt
            msg = wt.MSG()
        while not self._stop:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret in (0, -1):
                break
            if not msg.message == 786:
                continue
            self.pressed.emit(int(msg.wParam))
        for hid, _mods, _vk in bindings:
            user32.UnregisterHotKey(None, hid)

    def stop(self):
        self._stop = True
        if self._tid:
            try:
                ctypes.windll.user32.PostThreadMessageW(self._tid, 18, 0, 0)
            except Exception:
                return None
            return None

class OcrWorker(QObject):
    """一次识图任务：截屏 + 本地 OCR + （认不出时的）AI 请求。

    跑在 daemon 线程里、结果经队列交回主线程 emit —— 不用 QThread 是有原因的：
    关窗口时 AI 请求可能还挂在网络上（实测 4 s，超时上限 30 s），
    「运行中的 QThread 在进程退出时被销毁」实测直接 0xC0000409 / 0xC0000005
    （--hotkeytest 复现）。daemon 线程随进程结束，没有 Qt 线程要收尾。
    """
    ok = Signal(str, str, int)
    fail = Signal(str)
    def __init__(self, region, key, img=None, kind='grid', bank=None, pos_bank=None, local=True, learn=True, parent=None):
        super().__init__(parent)
        self.region = region
        self.key = key
        self.img = img
        self.kind = kind
        self.bank = bank
        self.pos_bank = pos_bank
        self.local = local
        self.learn = learn
        self._stop = False
        self._q = queue.Queue()
        self._thread = None
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._pump)

    def stop(self):
        """关程序时叫停：已经发出的 AI 请求收不回来，但还没发的那一步就别发了。"""
        self._stop = True

    def start(self):
        if self._thread is not None:
            return None
        self._thread = threading.Thread(target=self._work, name='ocr-worker', daemon=True)
        self._thread.start()
        self._timer.start()

    def isRunning(self):
        return bool((self._thread is not None and self._thread.is_alive()))

    def wait(self, ms=None):
        t = self._thread
        if t is None:
            return True
        t.join(None if ms is None else max(0.0, ms / 1000.0))
        return (not t.is_alive())

    def _pump(self):
        """主线程里把工作线程的结果 emit 出去（跨线程 emit 交给 Qt 队列，退出时容易踩空）。"""
        try:
            kind, payload = self._q.get_nowait()
        except queue.Empty:
            if not self.isRunning():
                self._timer.stop()
            return None
        self._timer.stop()
        if kind == 'ok':
            self.ok.emit(*payload)
        else:
            # fail 是 Signal(str)：以前这里直接 emit(payload) 传了个元组，
            # Shiboken 打一行 "Cannot copy-convert (tuple) to C++" 并把错误文字丢掉，
            # 界面上只剩「识别失败：」（--hotkeytest 里有这行噪音）。取元组里那一项再 emit。
            self.fail.emit(str(payload[0]) if payload else '')
        return None

    def _emit(self, kind, *payload):
        self._q.put((kind, payload))

    def _work(self):
        try:
            if self._stop:
                return None
            img = self.img if self.img is not None else vision.grab(tuple(self.region))
            note = ''
            if self.local and self.kind == 'az' and self.bank is not None:
                try:
                    txt, _, info = docr.read_az(img, self.bank)
                except Exception as e:
                    txt, _, info = (None, 0.0, f'本地出错:{e}')
                if txt:
                    try:
                        print(f"[ocr] kind={self.kind} 本地识别 -> {txt}", flush=True)
                    except Exception:
                        pass
                    self._emit('ok', txt, 'local', 0)
                    return None
                note = info
            elif self.local and self.kind == 'pos' and self.pos_bank is not None:
                try:
                    txt, _, info = posocr.read_pos(img, self.pos_bank)
                except Exception as e:
                    txt, _, info = (None, 0.0, f'本地出错:{e}')
                if txt:
                    try:
                        print(f"[ocr] kind={self.kind} 本地识别 -> {txt}", flush=True)
                    except Exception:
                        pass
                    self._emit('ok', txt, 'local', 0)
                    return None
                note = info
            if note:
                try:
                    print(f"[ocr] kind={self.kind} 本地放弃：{note}", flush=True)
                except Exception:
                    pass
            if not self.key:
                self._emit('ok', '', f"没填密钥，本地也没认出来（{note}）", 0)
                return None
            if self._stop:
                return None
            coord, raw, ms = vision.ask(img, self.key, self.kind)
            try:
                print(f"[ocr] kind={self.kind} AI {ms} ms -> {(coord or '（空）')}{'' if coord else (' · ' + (raw or '')[:80])}", flush=True)
            except Exception:
                pass
            if coord and self.learn and (self.kind == 'az') and (self.bank is not None):
                try:
                    docr.learn(coord, img, self.bank)
                except Exception:
                    pass
            elif coord and self.learn and (self.kind == 'pos') and (self.pos_bank is not None):
                try:
                    posocr.learn(coord, img, self.pos_bank)
                except Exception:
                    pass
            if note:
                raw = f"{raw}｜本地放弃：{note}"
            self._emit('ok', (coord or ''), raw, ms)
        except Exception as e:
            self._emit('fail', str(e))
        return None

_VK_SPECIAL = {Qt.Key_Space: 32, Qt.Key_Tab: 9, Qt.Key_Return: 13, Qt.Key_Enter: 13, Qt.Key_Insert: 45, Qt.Key_Delete: 46, Qt.Key_Home: 36, Qt.Key_End: 35, Qt.Key_PageUp: 33, Qt.Key_PageDown: 34, Qt.Key_Pause: 19, Qt.Key_Print: 44, Qt.Key_ScrollLock: 145, Qt.Key_Backspace: 8}

def seq_to_hotkey(seq):
    """QKeySequence -> (mods, vk)。不可用（空/纯修饰键/不支持的键）返回 None。"""
    if (seq is None or seq.isEmpty()):
        return None
    combo = seq[0]
    key = combo.key()
    km = combo.keyboardModifiers()
    mods = 0
    if km & Qt.ControlModifier:
        mods |= 2
    if km & Qt.ShiftModifier:
        mods |= 4
    if km & Qt.AltModifier:
        mods |= 1
    if km & Qt.MetaModifier:
        mods |= 8
    vk = None
    if Qt.Key_A <= key <= Qt.Key_Z:
        vk = (65 + (key - Qt.Key_A))
    elif Qt.Key_0 <= key <= Qt.Key_9:
        vk = (48 + (key - Qt.Key_0))
    elif Qt.Key_F1 <= key <= Qt.Key_F24:
        vk = (112 + (key - Qt.Key_F1))
    else:
        vk = _VK_SPECIAL.get(key)
    if vk is None:
        return None
    return (mods, vk)

def hotkey_to_text(text):
    """配置里的字符串规范成给人看的写法（F1 / Ctrl+Shift+D）。"""
    seq = QKeySequence(text)
    if seq.isEmpty():
        return 'F1'
    return (seq.toString(QKeySequence.NativeText) or text)

def parse_hotkey_text(text):
    seq = QKeySequence((text or 'F1'))
    got = seq_to_hotkey(seq)
    if got is None:
        got = seq_to_hotkey(QKeySequence('F1'))
    if got:
        return (got, seq.toString(QKeySequence.PortableText))
    return (got, 'F1')

HOTKEY_DEFS = ((1, 'hotkey', 'F1'), (2, 'hotkey_exact', 'F2'), (3, 'hotkey_clear', 'F3'), (4, 'hotkey_overlay', 'F4'), (11, 'r_hotkey', 'F5'), (12, 'r_clear', 'F6'), (13, 'r_overlay', 'F7'), (14, 'r_report', 'F11'), (21, 's_hotkey', 'F8'), (22, 's_clear', 'F9'), (23, 's_overlay', 'F10'), (31, 'c_hotkey', 'Alt+F12'), (32, 'c_clear', 'Alt+Shift+F12'), (33, 'c_overlay', 'Ctrl+F12'), (41, 'b_hotkey', 'Ctrl+F1'), (42, 'b_clear', 'Ctrl+Shift+F1'), (43, 'b_overlay', 'Ctrl+Alt+F1'))
COMBO_ID = 1000
ID_SET = {1: 'drone', 2: 'drone', 3: 'drone', 4: 'drone', 11: 'recon', 12: 'recon', 13: 'recon', 14: 'recon', 21: 'scope', 22: 'scope', 23: 'scope', 31: 'command', 32: 'command', 33: 'command', 41: 'bar', 42: 'bar', 43: 'bar'}
OVERLAY_IDS = (4, 13, 23, 33, 43)
SET_TITLE = {'drone': '间瞄·无人机视角', 'recon': '侦察报告', 'scope': '间瞄·望远镜视角', 'command': '发布命令', 'bar': '呼叫弹幕'}
ID_TITLE = {1: '快速定位', 2: '准确定位', 3: '清除坐标', 4: '浮窗显隐', 11: '侦察流程', 12: '清除目标', 13: '浮窗显隐', 14: '侦察报告', 21: '望远镜流程', 22: '清除望远镜', 23: '浮窗显隐', 31: '发布命令', 32: '取消输入', 33: '浮窗显隐', 41: '弹幕流程', 42: '清除弹幕', 43: '浮窗显隐'}
DIGIT_ID = 100
LETTER_ID = 200
RDIGIT_ID = 300
BACK_ID = 400
TOPDIGIT_ID = 500
VK_BACK = 8
VK_NUMPAD0 = 96
VK_NUMLOCK = 144

def numpad_vk(d):
    """数字一律取小键盘的虚拟键码，主键盘 0–9 不再被本程序占用。"""
    return (VK_NUMPAD0 + int(d))

def numlock_on():
    """小键盘当前是否发数字（NumLock 灯亮）。取不到状态时按“已开”处理。"""
    try:
        return bool((ctypes.windll.user32.GetKeyState(VK_NUMLOCK) & 1))
    except Exception:
        return True

class MainWindow(QMainWindow):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.arc = cfg.get('arc') if cfg.get('arc') in core.ARC_MODES else 'auto'
        self.maps = mapsdata.load_maps()
        print(f"[mapdb] {mapsdata.db_info()}", flush=True)
        self.weapons = core.load_weapons()
        self.los_store = los.LosStore()
        self.los_res = None
        # 阵地高度：地形 + 自动/手动抬升（桥面、屋顶、集装箱…）
        self.hgun = None            # 实际参与解算的炮位高（地形 + 抬升）
        self.gun_dz = 0.0           # 当前生效的抬升
        self.gun_dz_auto = 0.0      # 自动检测到的抬升
        self.gun_dz_label = ''      # 桥面 / 屋顶 / 集装箱…
        self._gun_dz_manual = None  # 手动覆盖值（None = 用自动）
        self._gun_dz_key = None     # 上次检测的阵地位置，换点就退回自动
        self._dz_lock = False       # 回填输入框时抑制 valueChanged
        # 目标高度：同一个道理（目标站在桥面 / 楼顶 / 集装箱上）
        self.htg = None             # 实际参与解算的目标高（地形 + 抬升）
        self.tgt_dz = 0.0
        self.tgt_dz_auto = 0.0
        self.tgt_dz_label = ''
        self._tgt_dz_manual = None
        self._tgt_dz_key = None
        self.gun = None
        self.target = None
        self.sol = None
        self.pend = []
        self.last_target = None
        self.tgt_mode = None
        self.copied = ''
        self.worker = None
        self.drill = None
        self.drill_az = None
        self.hotkeys = None
        self.ocr_mode = 'quick'
        self.ocr_kind = 'pos'
        self.pos_bank = posocr.open_bank((paths.user_dir() / 'position_bank.npz'), ((paths.resource_dir() / 'data') / 'position_bank.npz'))
        self.digit_bank = docr.open_bank((paths.user_dir() / 'digit_bank.npz'), ((paths.resource_dir() / 'data') / 'digit_bank_seed.npz'))
        try:
            print(f"[docr] 方位角模板库 {self.digit_bank.count()} 条", flush=True)
            print(f"[posocr] 坐标模板库 {self.pos_bank.count()} 条", flush=True)
        except Exception:
            pass
        self.tel = None
        self.drone = None
        self.arty_sub = cfg.get('arty_sub') if cfg.get('arty_sub') in ('drone', 'scope') else 'drone'
        self.tel_warn = ''
        self.mode = (cfg.get('mode', 'arty') or 'arty')
        self.recon = None
        self.recon_last = None
        self.ocr_flow = 'tel'
        self.ocr_what = ''
        _rs = cfg.get('recon_sub')
        self.recon_sub = _rs if _rs in ('drone', 'scope', 'map') else 'scope'
        self.recon_src = 'scout' if cfg.get('recon_src') == 'scout' else 'cmd'
        self.recon_targets = load_recon_targets()
        self.command_data = load_command_factions()
        self.cmd_faction = str((cfg.get('c_faction') or ''))
        self.cmd_unit = str((cfg.get('c_unit') or ''))
        self.cmd = None
        self.cmd_last = ''
        try:
            self.target_db = targetsdb.TargetDB((paths.user_dir() / 'targets.db'))
        except Exception as e:
            self.target_db = None
            print(f"[db] 目标库打不开：{e}", flush=True)
        self._digit_keys = ()
        self._nl_timer = QTimer(self)
        self._nl_timer.setInterval(1200)
        self._nl_timer.timeout.connect(self._numlock_hint)
        self.timer_start = None
        self._timer = QTimer(self)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._tick_timer)
        self._timer_clear = QTimer(self)
        self._timer_clear.setSingleShot(True)
        self._timer_clear.timeout.connect(self._clear_timer_text)
        self._char_keys = ()
        self.hk_specs = {}
        self.hk_texts = {}
        for _hid, _ckey, _dflt in HOTKEY_DEFS:
            _spec, _text = parse_hotkey_text(cfg.get(_ckey, _dflt))
            self.hk_specs[_hid] = _spec
            self.hk_texts[_hid] = _text
        self.setWindowTitle('Squad 间瞄火力助手')
        scr = QGuiApplication.primaryScreen().availableGeometry()
        w = min(1260, (scr.width() - 40))
        h = min(860, (scr.height() - 60))
        self.resize(w, h)
        self.move((scr.left() + ((scr.width() - w) // 2)), (scr.top() + max(8, ((scr.height() - h) // 2))))
        self.view = MapView()
        self.view.gun_set.connect(self.on_gun_set)
        self.view.locate_requested.connect(self.locate_dialog)
        self.view.grid_toggled.connect(self.on_grid_toggled)
        self.view.hd_ready.connect(self.on_hd_ready)
        self.view.flag_notice.connect(self.on_flag_notice)
        self.view.timer_clicked.connect(self.on_timer_clicked)
        self._build_panel()
        body = QWidget()
        lay = QHBoxLayout(body)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)
        lay.addWidget(self.view, 1)
        lay.addWidget(self.panel, 0)
        self.setCentralWidget(body)
        self.overlay = Overlay()
        if cfg.get('overlay'):
            self.overlay.move(*cfg['overlay'])
        else:
            self.overlay.reset_pos()
        self.overlay.moved_by_user.connect(self.on_overlay_moved)
        self.overlay.show_main.connect(self.show_main_window)
        self.overlay.show()
        self.overlay.setToolTip('拖住可移动 · 双击回到主界面 · 右键有菜单')
        self.overlay.set_values('—', '—', '—', '—', '先在地图上点出阵地')
        self.minimap = MiniMap()
        self.minimap.picked.connect(self.on_minimap_picked)
        self.minimap.closed.connect(self.on_minimap_closed)
        self.minimap.stage_pick.connect(self.on_bar_stage_pick)
        self.bar = None
        self._start_hotkeys()
        self.select_map(cfg.get('map', 'yehorivka'))
        self.select_weapon(cfg.get('weapon', 'Mortar#default'))
        self.on_map_changed(self.cb_map.currentIndex())
        self.on_weapon_changed(self.cb_weap.currentIndex())
        self.set_mode(self.mode)

    def _build_panel(self):
        self.panel = QFrame()
        self.panel.setFixedWidth(404)
        self.panel.setStyleSheet('QFrame{background:#11161d;} QLabel{color:#c7d2de;}QGroupBox{color:#ffd166;border:1px solid #24303d;border-radius:8px;margin-top:10px;padding:10px 8px 8px 8px;}QGroupBox::title{subcontrol-origin:margin;left:10px;padding:0 4px;}QComboBox,QLineEdit{background:#161d26;color:#e8eef5;border:1px solid #2b3947;border-radius:5px;padding:4px 6px;}QPushButton{background:#1b2430;color:#dce6f0;border:1px solid #2f3d4c;border-radius:6px;padding:6px 8px;}QPushButton:hover{background:#22303f;border-color:#3b7f96;}QPushButton#primary{background:#133240;border-color:#2f7d94;color:#9fe6f5;}QPushButton:checked{background:#133240;border-color:#2f7d94;color:#9fe6f5;font-weight:600;}QPushButton#danger{background:#3a1416;border-color:#8a2d2d;color:#ff9a9a;font-weight:600;}QPushButton#danger:hover{background:#4d1a1d;border-color:#c23c3c;color:#ffc7c7;}QComboBox QAbstractItemView{background:#161d26;color:#e8eef5;border:1px solid #2b3947;outline:0;selection-background-color:#1f4a5e;selection-color:#ffffff;}QComboBox QAbstractItemView::item{min-height:24px;padding-left:6px;color:#e8eef5;}QComboBox QAbstractItemView::item:selected{background:#1f4a5e;color:#ffffff;}QComboBox QAbstractItemView::item:hover{background:#1a3a49;color:#ffffff;}QListWidget{background:#161d26;color:#e8eef5;border:1px solid #2b3947;border-radius:5px;font-size:12px;}QListWidget::item{padding:3px 5px;color:#e8eef5;}QListWidget::item:hover{background:#1a3a49;color:#ffffff;}QListWidget::item:selected{background:#1f4a5e;color:#ffffff;}')
        self.inner = QWidget()
        self.inner.setStyleSheet('background:transparent;')
        v = QVBoxLayout(self.inner)
        v.setContentsMargins(6, 6, 6, 6)
        v.setSpacing(8)
        title = QLabel('间瞄火力计算')
        title.setStyleSheet('font-size:17px;font-weight:600;color:#ffd166;')
        v.addWidget(title)
        self.lb_sub = QLabel(self._sub_text())
        self.lb_sub.setWordWrap(True)
        self.lb_sub.setStyleSheet('color:#7f909f;font-size:11px;')
        v.addWidget(self.lb_sub)
        gb_mode = QGroupBox('模式')
        gm = QGridLayout(gb_mode)
        gm.setSpacing(6)
        self.btn_mode_arty = QPushButton('间瞄火力支援')
        self.btn_mode_recon = QPushButton('侦察报告')
        self.btn_mode_cmd = QPushButton('发布命令')
        self.btn_mode_bar = QPushButton('呼叫弹幕')
        self.grp_mode = QButtonGroup(self)
        self.grp_mode.setExclusive(True)
        for i, b in enumerate((self.btn_mode_arty, self.btn_mode_recon, self.btn_mode_cmd, self.btn_mode_bar)):
            b.setCheckable(True)
            b.setMinimumHeight(30)
            self.grp_mode.addButton(b)
            gm.addWidget(b, (i // 2), (i % 2))
        self.btn_mode_arty.clicked.connect((lambda: self.set_mode('arty')))
        self.btn_mode_recon.clicked.connect((lambda: self.set_mode('recon')))
        self.btn_mode_cmd.clicked.connect((lambda: self.set_mode('command')))
        self.btn_mode_bar.clicked.connect((lambda: self.set_mode('barrage')))
        v.addWidget(gb_mode)
        gb_sub = QGroupBox('间瞄子模式（两套截图框与键位互相独立）')
        gs = QHBoxLayout(gb_sub)
        self.btn_sub_drone = QPushButton('无人机视角')
        self.btn_sub_drone.setToolTip('只截目标坐标：快速定位 / 准确定位')
        self.btn_sub_scope = QPushButton('望远镜视角')
        self.btn_sub_scope.setToolTip('读你的方位角 + 你自己的坐标 + 距离，算出敌目标')
        self.grp_sub = QButtonGroup(self)
        self.grp_sub.setExclusive(True)
        for b in (self.btn_sub_drone, self.btn_sub_scope):
            b.setCheckable(True)
            b.setMinimumHeight(28)
            self.grp_sub.addButton(b)
            gs.addWidget(b)
        self.btn_sub_drone.clicked.connect((lambda: self.set_arty_sub('drone')))
        self.btn_sub_scope.clicked.connect((lambda: self.set_arty_sub('scope')))
        self.gb_sub = gb_sub
        v.addWidget(gb_sub)
        gb_key = QGroupBox('DeepSeek 密钥')
        g = QVBoxLayout(gb_key)
        row = QHBoxLayout()
        self.ed_key = QLineEdit(self.cfg.get('api_key', ''))
        self.ed_key.setEchoMode(QLineEdit.Password)
        self.ed_key.setPlaceholderText('sk-...')
        btn_save_key = QPushButton('保存')
        btn_save_key.clicked.connect(self.save_key)
        row.addWidget(self.ed_key, 1)
        row.addWidget(btn_save_key, 0)
        g.addLayout(row)
        self.lb_key = QLabel('模型 deepseek-flash（V4.1-Flash，读图）')
        self.lb_key.setStyleSheet('color:#7f909f;font-size:11px;')
        g.addWidget(self.lb_key)
        v.addWidget(gb_key)
        gb_reg = QGroupBox('截图区域 · 无人机视角')
        g = QVBoxLayout(gb_reg)
        lb_g = QLabel('目标坐标区域：无人机视角下的 CURRENT GRID（只框坐标文字）')
        lb_g.setStyleSheet('color:#9fe6f5;font-size:11px;')
        lb_g.setWordWrap(True)
        g.addWidget(lb_g)
        self.lb_region = QLabel()
        self.lb_region.setWordWrap(True)
        self.lb_region.setStyleSheet('font-family:Consolas;color:#9fe6f5;')
        g.addWidget(self.lb_region)
        row = QHBoxLayout()
        b1 = QPushButton('框选坐标区域')
        b1.clicked.connect((lambda: self.pick_region('grid')))
        b2 = QPushButton('预览截图')
        b2.clicked.connect((lambda: self.preview_region('grid')))
        b3 = QPushButton('预览识别')
        b3.setObjectName('primary')
        b3.clicked.connect(self.test_ocr_region)
        row.addWidget(b1)
        row.addWidget(b2)
        row.addWidget(b3)
        g.addLayout(row)
        row2 = QHBoxLayout()
        b4 = QPushButton('演练（假 HUD，端到端自测）')
        b4.clicked.connect(self.open_drill)
        row2.addWidget(b4)
        g.addLayout(row2)
        lb_w = QLabel('方位角区域：无人机画面上的方位/航向读数（快速定位先读它，读完再读目标坐标）')
        lb_w.setStyleSheet('color:#9fe6f5;font-size:11px;')
        lb_w.setWordWrap(True)
        g.addWidget(lb_w)
        roww = QHBoxLayout()
        roww.addWidget(QLabel('现在看的是'), 0)
        self.cb_drone_who = QComboBox()
        self.cb_drone_who.addItem('指挥官无人机', 'cmd')
        self.cb_drone_who.addItem('侦察兵无人机', 'scout')
        self.cb_drone_who.setCurrentIndex(1 if self.drone_who() == 'scout' else 0)
        self.cb_drone_who.setToolTip('快速定位先读这台无人机的方位角；两台各存一块框选区域')
        self.cb_drone_who.activated.connect((lambda i: self.set_drone_who(self.cb_drone_who.itemData(i))))
        roww.addWidget(self.cb_drone_who, 1)
        g.addLayout(roww)
        self.lb_d_az_cmd = QLabel()
        self.lb_d_az_scout = QLabel()
        for lb_a, who, label in ((self.lb_d_az_cmd, 'cmd', '指挥官无人机方位角'), (self.lb_d_az_scout, 'scout', '侦察兵无人机方位角')):
            lb_a.setStyleSheet('font-family:Consolas;color:#9fe6f5;font-size:11px;')
            lb_a.setWordWrap(True)
            g.addWidget(lb_a)
            ra = QHBoxLayout()
            ba1 = QPushButton(f"框选{label}")
            ba1.clicked.connect(lambda _=False, w=who: self.pick_region('d_az_scout' if w == 'scout' else 'd_az_cmd'))
            ba2 = QPushButton('预览')
            ba2.clicked.connect(lambda _=False, w=who: self.preview_region('d_az_scout' if w == 'scout' else 'd_az_cmd'))
            ra.addWidget(ba1)
            ra.addWidget(ba2)
            g.addLayout(ra)
        self.lb_drone_hint = QLabel(self._drone_hint())
        self.lb_drone_hint.setWordWrap(True)
        self.lb_drone_hint.setStyleSheet('color:#7f909f;font-size:11px;')
        g.addWidget(self.lb_drone_hint)
        rowp = QHBoxLayout()
        rowp.addWidget(QLabel('区域预设'), 0)
        self.cb_region = QComboBox()
        self.cb_region.setToolTip('保存过的框选区域，选中就套用')
        self.cb_region.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.cb_region.setMinimumContentsLength(4)
        self.cb_region.activated.connect((lambda i: self.on_region_preset(i, 'drone')))
        self.cb_region.view().setStyleSheet('QAbstractItemView{background:#161d26;color:#e8eef5;border:1px solid #2b3947;outline:0;selection-background-color:#1f4a5e;selection-color:#ffffff;}QAbstractItemView::item{min-height:24px;padding-left:6px;color:#e8eef5;}QAbstractItemView::item:selected{background:#1f4a5e;color:#ffffff;}')
        rowp.addWidget(self.cb_region, 1)
        g.addLayout(rowp)
        rowp2 = QHBoxLayout()
        bp1 = QPushButton('保存当前')
        bp1.setToolTip('把现在的框选区域存成预设，可命名')
        bp1.clicked.connect((lambda: self.save_region_preset('drone')))
        bp2 = QPushButton('重命名')
        bp2.clicked.connect((lambda: self.rename_region_preset('drone')))
        bp3 = QPushButton('删除')
        bp3.clicked.connect((lambda: self.delete_region_preset('drone')))
        rowp2.addWidget(bp1)
        rowp2.addWidget(bp2)
        rowp2.addWidget(bp3)
        g.addLayout(rowp2)
        self.qk_edits = {}
        for hid, label, tip in ((1, '快速定位', '截 1 次，直接把读到的坐标当目标'), (2, '准确定位', '截 3 次，取三点外心当目标'), (3, '清除坐标', '清掉截错/没凑齐的坐标（上次算好的目标保留）'), (4, '浮窗显隐', '显示 / 隐藏半透明浮窗')):
            row3 = QHBoxLayout()
            lb3 = QLabel(label)
            lb3.setMinimumWidth(58)
            lb3.setToolTip(tip)
            row3.addWidget(lb3, 0)
            ed = QKeySequenceEdit(QKeySequence(self.hk_texts[hid]))
            ed.setMaximumSequenceLength(1)
            ed.setToolTip('点一下这里，然后按下要用的键或组合键（如 F5、Ctrl+Shift+D、Alt+Q）')
            ed.editingFinished.connect((lambda h=hid: self.apply_hotkey(h)))
            row3.addWidget(ed, 1)
            g.addLayout(row3)
            self.qk_edits[hid] = ed
        self.lb_hk = QLabel(self._hk_hint())
        self.lb_hk.setWordWrap(True)
        self.lb_hk.setStyleSheet('color:#7f909f;font-size:11px;')
        g.addWidget(self.lb_hk)
        self.gb_reg = gb_reg
        v.addWidget(gb_reg)
        gb_scope = QGroupBox('截图区域 · 望远镜视角（独立预设与键位）')
        g = QVBoxLayout(gb_scope)
        lb_s1 = QLabel('① 玩家坐标区域：游戏里的 CURRENT GRID /「玩家位置：」')
        lb_s1.setStyleSheet('color:#9fe6f5;font-size:11px;')
        lb_s1.setWordWrap(True)
        g.addWidget(lb_s1)
        self.lb_s_region = QLabel()
        self.lb_s_region.setStyleSheet('font-family:Consolas;color:#9fe6f5;font-size:11px;')
        self.lb_s_region.setWordWrap(True)
        g.addWidget(self.lb_s_region)
        rs1 = QHBoxLayout()
        bs1 = QPushButton('框选玩家坐标')
        bs1.clicked.connect((lambda: self.pick_region('s_grid')))
        bs2 = QPushButton('预览')
        bs2.clicked.connect((lambda: self.preview_region('s_grid')))
        rs1.addWidget(bs1)
        rs1.addWidget(bs2)
        g.addLayout(rs1)
        lb_s2 = QLabel('② 方位角区域：望远镜里那几个数字（0–359，只框数字）')
        lb_s2.setStyleSheet('color:#9fe6f5;font-size:11px;')
        lb_s2.setWordWrap(True)
        g.addWidget(lb_s2)
        self.lb_s_az = QLabel()
        self.lb_s_az.setStyleSheet('font-family:Consolas;color:#9fe6f5;font-size:11px;')
        self.lb_s_az.setWordWrap(True)
        g.addWidget(self.lb_s_az)
        rs2 = QHBoxLayout()
        bs3 = QPushButton('框选方位角')
        bs3.clicked.connect((lambda: self.pick_region('s_az')))
        bs4 = QPushButton('预览')
        bs4.clicked.connect((lambda: self.preview_region('s_az')))
        rs2.addWidget(bs3)
        rs2.addWidget(bs4)
        g.addLayout(rs2)
        rsp = QHBoxLayout()
        rsp.addWidget(QLabel('区域预设（坐标+方位角）'), 0)
        self.cb_region_s = QComboBox()
        self.cb_region_s.setToolTip('望远镜专用预设，和无人机视角那套分开存')
        self.cb_region_s.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.cb_region_s.setMinimumContentsLength(4)
        self.cb_region_s.activated.connect((lambda i: self.on_region_preset(i, 'scope')))
        self.cb_region_s.view().setStyleSheet('QAbstractItemView{background:#161d26;color:#e8eef5;border:1px solid #2b3947;outline:0;selection-background-color:#1f4a5e;selection-color:#ffffff;}QAbstractItemView::item{min-height:24px;padding-left:6px;color:#e8eef5;}QAbstractItemView::item:selected{background:#1f4a5e;color:#ffffff;}')
        rsp.addWidget(self.cb_region_s, 1)
        g.addLayout(rsp)
        rsp2 = QHBoxLayout()
        sp1 = QPushButton('保存当前')
        sp1.clicked.connect((lambda: self.save_region_preset('scope')))
        sp2 = QPushButton('重命名')
        sp2.clicked.connect((lambda: self.rename_region_preset('scope')))
        sp3 = QPushButton('删除')
        sp3.clicked.connect((lambda: self.delete_region_preset('scope')))
        rsp2.addWidget(sp1)
        rsp2.addWidget(sp2)
        rsp2.addWidget(sp3)
        g.addLayout(rsp2)
        for hid, label, tip in ((21, '望远镜流程', '开始 / 下一步：读方位角 → 读你的坐标 → 补第 4 段 → 输 3 位距离'), (22, '清除望远镜', '清掉这次没走完的望远镜流程（上次算好的目标保留）'), (23, '浮窗显隐', '显示 / 隐藏半透明浮窗（望远镜专用键）')):
            rows = QHBoxLayout()
            lbs = QLabel(label)
            lbs.setMinimumWidth(70)
            lbs.setToolTip(tip)
            rows.addWidget(lbs, 0)
            eds = QKeySequenceEdit(QKeySequence(self.hk_texts[hid]))
            eds.setMaximumSequenceLength(1)
            eds.setToolTip('点一下这里，然后按下要用的键或组合键')
            eds.editingFinished.connect((lambda h=hid: self.apply_hotkey(h)))
            rows.addWidget(eds, 1)
            g.addLayout(rows)
            self.qk_edits[hid] = eds
        self.lb_scope = QLabel(self._scope_hint())
        self.lb_scope.setWordWrap(True)
        self.lb_scope.setStyleSheet('color:#7f909f;font-size:11px;')
        g.addWidget(self.lb_scope)
        self.btn_tel_cancel = QPushButton('取消望远镜流程')
        self.btn_tel_cancel.setToolTip('清掉这次没走完的流程（上次算好的目标保留）')
        self.btn_tel_cancel.clicked.connect((lambda: self.tel_reset(True, '已取消望远镜流程')))
        g.addWidget(self.btn_tel_cancel)
        self.gb_scope = gb_scope
        v.addWidget(gb_scope)
        gb_alg = QGroupBox('地图与武器')
        g = QGridLayout(gb_alg)
        g.addWidget(QLabel('地图'), 0, 0)
        self.cb_map = QComboBox()
        self.cb_map.currentIndexChanged.connect(self.on_map_changed)
        g.addWidget(self.cb_map, 0, 1, 1, 2)
        g.addWidget(QLabel('武器'), 1, 0)
        self.cb_weap = QComboBox()
        self.cb_weap.currentIndexChanged.connect(self.on_weapon_changed)
        g.addWidget(self.cb_weap, 1, 1, 1, 2)
        g.addWidget(QLabel('弹道'), 2, 0)
        arc_row = QHBoxLayout()
        arc_row.setSpacing(4)
        self.grp_arc = QButtonGroup(self)
        self.grp_arc.setExclusive(True)
        self.btns_arc = {}
        for _key, _text, _tip in (
                ('auto', '自动', '按武器自己的习惯取解：迫击炮 / 榴弹炮走高抛，火箭巢 / 车载炮 / 榴弹发射器走低抛（跟以前一样）'),
                ('low', '低抛', '强制低抛（平射那一支解）：弹道平、飞行时间短，前方不能有山脊 / 建筑挡着'),
                ('high', '高抛', '强制高抛（大仰角那一支解）：弹道拱起来越过头顶的掩体，飞行时间长、散布更大')):
            _b = QPushButton(_text)
            _b.setCheckable(True)
            _b.setCursor(Qt.PointingHandCursor)
            _b.setMinimumHeight(26)
            _b.setToolTip(_tip)
            _b.clicked.connect((lambda _=False, k=_key: self.set_arc(k)))
            self.grp_arc.addButton(_b)
            self.btns_arc[_key] = _b
            arc_row.addWidget(_b, 1)
        g.addLayout(arc_row, 2, 1, 1, 2)
        g.addWidget(QLabel('阵地库'), 3, 0)
        self.cb_bar_gun = QComboBox()
        self.cb_bar_gun.currentIndexChanged.connect(self.on_bar_gun_picked)
        g.addWidget(self.cb_bar_gun, 3, 1, 1, 2)
        self.cb_map.blockSignals(True)
        self.cb_weap.blockSignals(True)
        for i, m in enumerate(self.maps):
            self.cb_map.addItem(m.label, i)
        for w in self.weapons:
            self.cb_weap.addItem(w['name'], w['key'])
        self.cb_map.blockSignals(False)
        self.cb_weap.blockSignals(False)
        for cb in (self.cb_map, self.cb_weap, self.cb_bar_gun):
            cb.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            cb.setMinimumContentsLength(4)
            cb.view().setStyleSheet('QAbstractItemView{background:#161d26;color:#e8eef5;border:1px solid #2b3947;outline:0;selection-background-color:#1f4a5e;selection-color:#ffffff;}QAbstractItemView::item{min-height:24px;padding-left:6px;color:#e8eef5;}QAbstractItemView::item:selected{background:#1f4a5e;color:#ffffff;}')
        g.setColumnStretch(1, 1)
        self.gb_alg = gb_alg
        self.btns_arc[self.arc].setChecked(True)
        v.addWidget(gb_alg)
        gb_recon = QGroupBox('侦察报告（键位与截图框独立）')
        g = QVBoxLayout(gb_recon)
        rsub = QHBoxLayout()
        self.btn_rsub_drone = QPushButton('无人机视角')
        self.btn_rsub_drone.setToolTip('只截敌目标坐标 → 小键盘补第 4 位 → 再按标记键确认 → 浮窗里选敌方目标')
        self.btn_rsub_scope = QPushButton('望远镜视角')
        self.btn_rsub_scope.setToolTip('读你的方位角 + 坐标 + 距离，算出敌目标（旧流程）')
        self.btn_rsub_map = QPushButton('地图视角')
        self.btn_rsub_map.setToolTip('不截图：按标记键后直接键入目标的 4 级格号，输完再按标记键确认')
        self.grp_rsub = QButtonGroup(self)
        self.grp_rsub.setExclusive(True)
        for b in (self.btn_rsub_drone, self.btn_rsub_scope, self.btn_rsub_map):
            b.setCheckable(True)
            b.setMinimumHeight(28)
            self.grp_rsub.addButton(b)
            rsub.addWidget(b)
        self.btn_rsub_drone.clicked.connect((lambda: self.set_recon_sub('drone')))
        self.btn_rsub_scope.clicked.connect((lambda: self.set_recon_sub('scope')))
        self.btn_rsub_map.clicked.connect((lambda: self.set_recon_sub('map')))
        g.addLayout(rsub)
        self.w_rreg_drone = QWidget()
        gd = QVBoxLayout(self.w_rreg_drone)
        gd.setContentsMargins(0, 0, 0, 0)
        ld = QLabel('敌目标坐标区域：无人机视角下的 CURRENT GRID（只框坐标文字）')
        ld.setStyleSheet('color:#9fe6f5;font-size:11px;')
        ld.setWordWrap(True)
        gd.addWidget(ld)
        self.grp_rsrc = QButtonGroup(self)
        self.grp_rsrc.setExclusive(True)
        rs = QHBoxLayout()
        self.btn_rsrc_cmd = QPushButton('指挥官无人机')
        self.btn_rsrc_scout = QPushButton('侦察兵无人机')
        for b in (self.btn_rsrc_cmd, self.btn_rsrc_scout):
            b.setCheckable(True)
            b.setMinimumHeight(26)
            self.grp_rsrc.addButton(b)
            rs.addWidget(b)
        self.btn_rsrc_cmd.clicked.connect((lambda: self.set_recon_src('cmd')))
        self.btn_rsrc_scout.clicked.connect((lambda: self.set_recon_src('scout')))
        gd.addLayout(rs)
        self.lb_recon_tgt = QLabel()
        self.lb_recon_tgt.setStyleSheet('font-family:Consolas;color:#9fe6f5;font-size:11px;')
        self.lb_recon_tgt.setWordWrap(True)
        gd.addWidget(self.lb_recon_tgt)
        rt0 = QHBoxLayout()
        tb1 = QPushButton('框选目标坐标')
        tb1.clicked.connect((lambda: self.pick_region('r_tgt')))
        tb2 = QPushButton('预览')
        tb2.clicked.connect((lambda: self.preview_region('r_tgt')))
        rt0.addWidget(tb1)
        rt0.addWidget(tb2)
        gd.addLayout(rt0)
        ld2 = QLabel('方位角区域（可选）：无人机画面上的方位读数。框了它，标记键就变成两步（指挥官无人机：先读目标坐标 → 再读方位角；侦察兵无人机反过来），读完会弹 60 m 小地图让你点出真实位置（指挥官那张图用刚读到的最新方位角）；用的是上面「间瞄火力支援·无人机视角」那两块框。')
        ld2.setStyleSheet('color:#9fe6f5;font-size:11px;')
        ld2.setWordWrap(True)
        gd.addWidget(ld2)
        self.lb_recon_az_d = QLabel()
        self.lb_recon_az_d.setStyleSheet('font-family:Consolas;color:#9fe6f5;font-size:11px;')
        self.lb_recon_az_d.setWordWrap(True)
        gd.addWidget(self.lb_recon_az_d)
        rt1 = QHBoxLayout()
        tb3 = QPushButton('框选方位角（当前无人机）')
        tb3.clicked.connect(self.pick_recon_drone_az)
        tb4 = QPushButton('预览')
        tb4.clicked.connect(self.preview_recon_drone_az)
        rt1.addWidget(tb3)
        rt1.addWidget(tb4)
        gd.addLayout(rt1)
        g.addWidget(self.w_rreg_drone)
        self.w_rreg_scope = QWidget()
        gk = QVBoxLayout(self.w_rreg_scope)
        gk.setContentsMargins(0, 0, 0, 0)
        lb1 = QLabel('① 方位角区域：你视角里那几个数字（0–359）')
        lb1.setStyleSheet('color:#9fe6f5;font-size:11px;')
        lb1.setWordWrap(True)
        gk.addWidget(lb1)
        self.lb_recon_az = QLabel()
        self.lb_recon_az.setStyleSheet('font-family:Consolas;color:#9fe6f5;font-size:11px;')
        self.lb_recon_az.setWordWrap(True)
        gk.addWidget(self.lb_recon_az)
        r1 = QHBoxLayout()
        rb1 = QPushButton('框选侦察方位角')
        rb1.clicked.connect((lambda: self.pick_region('r_az')))
        rb2 = QPushButton('预览')
        rb2.clicked.connect((lambda: self.preview_region('r_az')))
        r1.addWidget(rb1)
        r1.addWidget(rb2)
        gk.addLayout(r1)
        lb2 = QLabel('② 玩家坐标区域：游戏里的 CURRENT GRID /「玩家位置：」')
        lb2.setStyleSheet('color:#9fe6f5;font-size:11px;')
        lb2.setWordWrap(True)
        gk.addWidget(lb2)
        self.lb_recon_region = QLabel()
        self.lb_recon_region.setStyleSheet('font-family:Consolas;color:#9fe6f5;font-size:11px;')
        self.lb_recon_region.setWordWrap(True)
        gk.addWidget(self.lb_recon_region)
        r2 = QHBoxLayout()
        rb3 = QPushButton('框选侦察坐标')
        rb3.clicked.connect((lambda: self.pick_region('r_grid')))
        rb4 = QPushButton('预览')
        rb4.clicked.connect((lambda: self.preview_region('r_grid')))
        r2.addWidget(rb3)
        r2.addWidget(rb4)
        gk.addLayout(r2)
        g.addWidget(self.w_rreg_scope)
        self.w_rreg_map = QLabel('地图视角不用框区域：按标记键后直接键入目标的 4 级格号（字母 + 行号 + 三位子格，如 K12-1-8-8），输完再按一次标记键确认')
        self.w_rreg_map.setStyleSheet('color:#9fe6f5;font-size:11px;')
        self.w_rreg_map.setWordWrap(True)
        g.addWidget(self.w_rreg_map)
        rf = QHBoxLayout()
        rf.addWidget(QLabel('敌方阵营'), 0)
        self.cb_faction = QComboBox()
        self.cb_faction.setToolTip('先选好敌人是谁：标记目标时的选项就来自这套编制表')
        self.cb_faction.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.cb_faction.setMinimumContentsLength(4)
        self.cb_faction.view().setStyleSheet('QAbstractItemView{background:#161d26;color:#e8eef5;border:1px solid #2b3947;outline:0;selection-background-color:#1f4a5e;selection-color:#ffffff;}QAbstractItemView::item{min-height:24px;padding-left:6px;color:#e8eef5;}QAbstractItemView::item:selected{background:#1f4a5e;color:#ffffff;}')
        self.cb_faction.activated.connect(self.on_faction_changed)
        rf.addWidget(self.cb_faction, 1)
        g.addLayout(rf)
        rl = QHBoxLayout()
        self.lb_targets = QLabel('目标库：0 条')
        self.lb_targets.setStyleSheet('color:#7f909f;font-size:11px;')
        self.lb_targets.setWordWrap(True)
        rl.addWidget(self.lb_targets, 1)
        self.btn_targets_clear = QPushButton('清空目标库')
        self.btn_targets_clear.clicked.connect(self.clear_targets)
        rl.addWidget(self.btn_targets_clear, 0)
        g.addLayout(rl)
        for hid, label, tip in ((11, '标记 / 下一步', '无人机：截敌目标坐标；望远镜：读方位角 → 坐标 → 距离。之后选敌方目标'), (14, '报告', '从目标库里挑一个已记录的目标 + 输队友坐标，算出他该看哪、多远'), (12, '清除目标', '选目标 / 全部清除；清之前自动把「型号 UID 已经被消灭；」复制进剪贴板'), (13, '浮窗显隐', '显示 / 隐藏半透明浮窗（侦察模式专用键）')):
            row4 = QHBoxLayout()
            lb4 = QLabel(label)
            lb4.setMinimumWidth(58)
            lb4.setToolTip(tip)
            row4.addWidget(lb4, 0)
            ed = QKeySequenceEdit(QKeySequence(self.hk_texts[hid]))
            ed.setMaximumSequenceLength(1)
            ed.setToolTip('点一下这里，然后按下要用的键或组合键')
            ed.editingFinished.connect((lambda h=hid: self.apply_hotkey(h)))
            row4.addWidget(ed, 1)
            g.addLayout(row4)
            self.qk_edits[hid] = ed
        self.lb_recon = QLabel(self._recon_hint())
        self.lb_recon.setWordWrap(True)
        self.lb_recon.setStyleSheet('color:#7f909f;font-size:11px;')
        g.addWidget(self.lb_recon)
        rr = QHBoxLayout()
        self.btn_recon_step = QPushButton(f"标记 / 下一步（{self.hk_texts[11]}）")
        self.btn_recon_step.setObjectName('primary')
        self.btn_recon_step.clicked.connect(self.recon_step)
        self.btn_recon_report = QPushButton(f"报告（{self.hk_texts[14]}）")
        self.btn_recon_report.clicked.connect(self.recon_report)
        rr.addWidget(self.btn_recon_step)
        rr.addWidget(self.btn_recon_report)
        g.addLayout(rr)
        rr2 = QHBoxLayout()
        self.btn_recon_clear = QPushButton('清除侦察目标（可选一个/几个/全部）')
        self.btn_recon_clear.clicked.connect(self.recon_clear_menu)
        rr2.addWidget(self.btn_recon_clear)
        g.addLayout(rr2)
        self.gb_recon = gb_recon
        v.addWidget(gb_recon)
        gb_cmd = QGroupBox('发布命令（我方阵营 / 部队）')
        g = QVBoxLayout(gb_cmd)
        _view_qss = 'QAbstractItemView{background:#161d26;color:#e8eef5;border:1px solid #2b3947;outline:0;selection-background-color:#1f4a5e;selection-color:#ffffff;}QAbstractItemView::item{min-height:24px;padding-left:6px;color:#e8eef5;}QAbstractItemView::item:selected{background:#1f4a5e;color:#ffffff;}'
        cf = QHBoxLayout()
        cf.addWidget(QLabel('我方阵营'), 0)
        self.cb_cfaction = QComboBox()
        self.cb_cfaction.setToolTip('先选我方是哪个阵营：建造选项来自这套编制表')
        self.cb_cfaction.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.cb_cfaction.setMinimumContentsLength(4)
        self.cb_cfaction.view().setStyleSheet(_view_qss)
        self.cb_cfaction.activated.connect(self.on_cmd_faction_changed)
        cf.addWidget(self.cb_cfaction, 1)
        g.addLayout(cf)
        cu = QHBoxLayout()
        cu.addWidget(QLabel('部队'), 0)
        self.cb_cunit = QComboBox()
        self.cb_cunit.setToolTip('必须选到具体部队：同一阵营不同部队的可建造项不一样')
        self.cb_cunit.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.cb_cunit.setMinimumContentsLength(4)
        self.cb_cunit.view().setStyleSheet(_view_qss)
        self.cb_cunit.activated.connect(self.on_cmd_unit_changed)
        cu.addWidget(self.cb_cunit, 1)
        g.addLayout(cu)
        self.lb_cunit = QLabel('—')
        self.lb_cunit.setStyleSheet('color:#9fe6f5;font-size:11px;')
        self.lb_cunit.setWordWrap(True)
        g.addWidget(self.lb_cunit)
        self.lb_cmd_step = QLabel(self._cmd_hint())
        self.lb_cmd_step.setStyleSheet('color:#ffd166;font-size:12px;')
        self.lb_cmd_step.setWordWrap(True)
        g.addWidget(self.lb_cmd_step)
        self.lb_cmd_warn = QLabel('')
        self.lb_cmd_warn.setStyleSheet('color:#ff8f8f;font-size:11px;')
        self.lb_cmd_warn.setWordWrap(True)
        self.lb_cmd_warn.setVisible(False)
        g.addWidget(self.lb_cmd_warn)
        self.lb_cmd_hint = QLabel(self._cmd_tip())
        self.lb_cmd_hint.setStyleSheet('color:#7f909f;font-size:11px;')
        self.lb_cmd_hint.setWordWrap(True)
        g.addWidget(self.lb_cmd_hint)
        self.lb_cmd_text = QLabel('')
        self.lb_cmd_text.setStyleSheet('font-family:Consolas;font-size:12px;color:#9fe6f5;')
        self.lb_cmd_text.setWordWrap(True)
        self.lb_cmd_text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        g.addWidget(self.lb_cmd_text)
        for hid, label, tip in ((31, '发布命令 / 确认', '开始发布命令；每一步输完再按一下确认（小键盘输数字）'), (32, '取消已输入', '清掉这次发布命令已经输入的全部内容，重新来'), (33, '浮窗显隐', '显示 / 隐藏半透明浮窗（发布命令专用键）')):
            row4 = QHBoxLayout()
            lb4 = QLabel(label)
            lb4.setMinimumWidth(58)
            lb4.setToolTip(tip)
            row4.addWidget(lb4, 0)
            ed = QKeySequenceEdit(QKeySequence(self.hk_texts[hid]))
            ed.setMaximumSequenceLength(1)
            ed.setToolTip('点一下这里，然后按下要用的键或组合键')
            ed.editingFinished.connect((lambda h=hid: self.apply_hotkey(h)))
            row4.addWidget(ed, 1)
            g.addLayout(row4)
            self.qk_edits[hid] = ed
        cr = QHBoxLayout()
        self.btn_cmd_run = QPushButton('发布命令 / 确认')
        self.btn_cmd_run.setObjectName('primary')
        self.btn_cmd_run.setToolTip('开始发布命令 / 确认当前这一步')
        self.btn_cmd_run.clicked.connect(self.cmd_step)
        self.btn_cmd_clear = QPushButton('取消已输入的内容')
        self.btn_cmd_clear.setToolTip('清掉这次发布命令已经输入的内容')
        self.btn_cmd_clear.clicked.connect((lambda: self.cmd_reset('已取消已输入的内容')))
        cr.addWidget(self.btn_cmd_run)
        cr.addWidget(self.btn_cmd_clear)
        g.addLayout(cr)
        self.btn_cmd_overlay = QPushButton('浮窗显隐')
        self.btn_cmd_overlay.setToolTip('显示 / 隐藏半透明浮窗')
        self.btn_cmd_overlay.clicked.connect(self.toggle_overlay)
        g.addWidget(self.btn_cmd_overlay)
        self.gb_cmd = gb_cmd
        v.addWidget(gb_cmd)
        gb_bar = QGroupBox('呼叫弹幕（阵地库 · 起始点 · 宽度 · 方向 · 推进速度）')
        gbv = QVBoxLayout(gb_bar)
        gbv.setSpacing(8)
        self.lb_bar_lib = QLabel('阵地库（最多 15 个；勾选的要参与这次弹幕）')
        self.lb_bar_lib.setWordWrap(True)
        gbv.addWidget(self.lb_bar_lib)
        self.lw_bar_pos = QListWidget()
        self.lw_bar_pos.setFixedHeight(140)
        self.lw_bar_pos.itemChanged.connect(self.on_bar_item_changed)
        gbv.addWidget(self.lw_bar_pos)
        self.lb_bar_pos = QLabel('0 / 15 个阵地 · 勾选 0 个参与')
        self.lb_bar_pos.setStyleSheet('color:#7f909f;font-size:11px;')
        self.lb_bar_pos.setWordWrap(True)
        gbv.addWidget(self.lb_bar_pos)
        gb1 = QHBoxLayout()
        self.btn_bar_add = QPushButton('新建阵地')
        self.btn_bar_edit = QPushButton('修改')
        self.btn_bar_del = QPushButton('删除')
        for b in (self.btn_bar_add, self.btn_bar_edit, self.btn_bar_del):
            gb1.addWidget(b)
        self.btn_bar_add.clicked.connect(self.bar_add_position)
        self.btn_bar_edit.clicked.connect(self.bar_edit_position)
        self.btn_bar_del.clicked.connect(self.bar_del_position)
        gbv.addLayout(gb1)
        self.lb_bar_sub = QLabel('起始点怎么标（这一环节与间瞄火力支援同一套截图框）')
        self.lb_bar_sub.setWordWrap(True)
        gbv.addWidget(self.lb_bar_sub)
        gb2 = QHBoxLayout()
        self.btn_bar_drone = QPushButton('无人机视角')
        self.btn_bar_scope = QPushButton('望远镜视角')
        self.grp_bar_sub = QButtonGroup(self)
        self.grp_bar_sub.setExclusive(True)
        for b in (self.btn_bar_drone, self.btn_bar_scope):
            b.setCheckable(True)
            b.setMinimumHeight(28)
            self.grp_bar_sub.addButton(b)
            gb2.addWidget(b)
        self.btn_bar_drone.clicked.connect((lambda: self.set_bar_sub('drone')))
        self.btn_bar_scope.clicked.connect((lambda: self.set_bar_sub('scope')))
        gbv.addLayout(gb2)
        self.lb_bar_hint = QLabel('')
        self.lb_bar_hint.setStyleSheet('color:#7f909f;font-size:11px;')
        self.lb_bar_hint.setWordWrap(True)
        gbv.addWidget(self.lb_bar_hint)
        self.lb_bar_step = QLabel('')
        self.lb_bar_step.setStyleSheet('color:#ffd166;font-size:12px;')
        self.lb_bar_step.setWordWrap(True)
        gbv.addWidget(self.lb_bar_step)
        self.lb_bar_text = QLabel('')
        self.lb_bar_text.setStyleSheet('color:#d6e4f0;font-family:Consolas;font-size:12px;')
        self.lb_bar_text.setWordWrap(True)
        self.lb_bar_text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        gbv.addWidget(self.lb_bar_text)
        gb3 = QHBoxLayout()
        self.btn_bar_prev = QPushButton('上一轮')
        self.btn_bar_next = QPushButton('下一轮')
        self.btn_bar_copy = QPushButton('复制本轮')
        self.btn_bar_clear = QPushButton('清除')
        for b in (self.btn_bar_prev, self.btn_bar_next, self.btn_bar_copy, self.btn_bar_clear):
            gb3.addWidget(b)
        self.btn_bar_prev.clicked.connect(self.bar_prev)
        self.btn_bar_next.clicked.connect(self.bar_next)
        self.btn_bar_copy.clicked.connect(self.bar_copy)
        self.btn_bar_clear.clicked.connect((lambda: self.bar_clear('已清除呼叫弹幕')))
        gbv.addLayout(gb3)
        for hid, label, tip in ((41, '弹幕流程 / 确认', '开始 · 确认推进速度 · 下一轮'), (42, '清除弹幕', '取消整次弹幕'), (43, '浮窗显隐', '显示/隐藏浮窗')):
            row = QHBoxLayout()
            lb = QLabel(label)
            lb.setMinimumWidth(120)
            ed = QKeySequenceEdit()
            ed.setMaximumSequenceLength(1)
            ed.setToolTip(tip)
            ed.editingFinished.connect((lambda h=hid: self.apply_hotkey(h)))
            self.qk_edits[hid] = ed
            row.addWidget(lb)
            row.addWidget(ed)
            gbv.addLayout(row)
        self.lb_bar_src = QLabel('起始点：无人机视角=读屏幕上的坐标；望远镜视角=方位角 + 你的坐标 + 距离。宽度/方向都在同一张小地图上点，中间不关窗口。')
        self.lb_bar_src.setWordWrap(True)
        self.lb_bar_src.setStyleSheet('color:#7f909f;font-size:11px;')
        self.gb_bar = gb_bar
        v.addWidget(gb_bar)
        gb_res = QGroupBox('诸元')
        g = QGridLayout(gb_res)
        self.vals = {}
        self.lb_angle = None
        for row, (key, label) in enumerate((('gun', '阵地坐标'), ('tgt', '目标坐标'), ('brg', '方向角'), ('mil', '密位'))):
            lb = QLabel(label)
            lb.setStyleSheet('color:#8fa1b2;')
            if key == 'mil':
                self.lb_angle = lb
            val = QLabel('—')
            val.setStyleSheet('font-family:Consolas;font-size:19px;font-weight:700;color:#fff;')
            val.setTextInteractionFlags(Qt.TextSelectableByMouse)
            g.addWidget(lb, row, 0)
            g.addWidget(val, row, 1)
            self.vals[key] = val
        self.lb_extra = QLabel('')
        self.lb_extra.setStyleSheet('color:#8fa1b2;font-size:11px;')
        self.lb_extra.setWordWrap(True)

        def _dz_row(title, tip, spin_attr, lab_attr, btn_attr, on_change, on_reset, row):
            """一行「XXX 高出地面 [输入] [自动] 说明」；阵地和目标各来一份。"""
            box = QHBoxLayout()
            lb = QLabel(title)
            lb.setStyleSheet('color:#8fa1b2;')
            sp = QDoubleSpinBox()
            sp.setRange(0.0, 40.0)
            sp.setDecimals(1)
            sp.setSingleStep(0.5)
            sp.setSuffix(' m')
            sp.setMaximumWidth(96)
            sp.setToolTip(tip)
            sp.valueChanged.connect(on_change)
            lab = QLabel('自动 · 地形')
            lab.setStyleSheet('color:#7f909f;font-size:11px;')
            btn = QPushButton('自动')
            btn.setMaximumWidth(52)
            btn.setToolTip('丢掉手动值，回到「按桥面/集装箱自动判定」')
            btn.clicked.connect(on_reset)
            box.addWidget(lb)
            box.addWidget(sp)
            box.addWidget(btn)
            box.addWidget(lab, 1)
            setattr(self, spin_attr, sp)
            setattr(self, lab_attr, lab)
            setattr(self, btn_attr, btn)
            return box

        g.addLayout(_dz_row('阵地高出地面',
                            '炮位比地形高多少米（站在桥面 / 集装箱上时用）。'
                            '默认自动：阵地落在桥面 / 集装箱 / 货箱上会自动抬高；'
                            '手动改了就以你填的为准，「自动」按钮恢复。',
                            'spin_dz', 'lb_dz', 'btn_dz_auto',
                            self.on_gun_dz_changed, self.gun_dz_reset, 4), 4, 0, 1, 2)
        g.addLayout(_dz_row('目标高出地面',
                            '目标比地形高多少米（目标在楼顶 / 桥面 / 集装箱上时用）。'
                            '默认自动只认桥面 / 集装箱 / 货箱；楼顶这类整栋楼的包围盒不会自动抬，'
                            '面板会写「脚下房屋 N m」，站上去就手填那个数。',
                            'spin_tdz', 'lb_tdz', 'btn_tdz_auto',
                            self.on_tgt_dz_changed, self.tgt_dz_reset, 5), 5, 0, 1, 2)
        g.addWidget(self.lb_extra, 6, 0, 1, 2)
        self.lb_status = QLabel(f"先在地图上点一下设阵地，再按 {self.hk_text} 快速定位（或 {self.hk_texts[2]} 准确定位）")
        self.lb_status.setWordWrap(True)
        self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
        g.addWidget(self.lb_status, 7, 0, 1, 2)
        g.setColumnStretch(1, 1)
        self.gb_res = gb_res
        v.addWidget(gb_res)
        gb_rres = QGroupBox('侦察报告')
        g = QGridLayout(gb_rres)
        self.rvals = {}
        for row, (key, label) in enumerate((('mode', '模式'), ('me', '我'), ('measured', '我测得'), ('mate', '队友'), ('mateview', '队友看'), ('tg', '目标'))):
            lb = QLabel(label)
            lb.setStyleSheet('color:#8fa1b2;')
            val = QLabel('—')
            val.setStyleSheet('font-family:Consolas;font-size:15px;font-weight:700;color:#fff;')
            val.setTextInteractionFlags(Qt.TextSelectableByMouse)
            val.setWordWrap(True)
            g.addWidget(lb, row, 0)
            g.addWidget(val, row, 1)
            self.rvals[key] = val
        g.setColumnStretch(1, 1)
        self.gb_recon_res = gb_rres
        v.addWidget(gb_rres)
        self.lb_recon_report = QLabel('')
        self.lb_recon_report.setStyleSheet('color:#9fe6f5;font-size:11px;')
        self.lb_recon_report.setWordWrap(True)
        v.addWidget(self.lb_recon_report)
        rowb = QHBoxLayout()
        self.btn_manual = QPushButton(f"快速定位（{self.hk_text}）")
        b5 = self.btn_manual
        b5.setObjectName('primary')
        b5.setToolTip('等同按一次快速定位键：截 1 次直接出结果')
        b5.clicked.connect((lambda: self.start_ocr('quick')))
        self.btn_exact = QPushButton(f"准确定位（{self.hk_texts[2]}）")
        self.btn_exact.setToolTip('等同按准确定位键：截 3 次取三点外心')
        self.btn_exact.clicked.connect((lambda: self.start_ocr('exact')))
        rowb.addWidget(b5)
        rowb.addWidget(self.btn_exact)
        v.addLayout(rowb)
        rowb2 = QHBoxLayout()
        self.btn_reset_pending = QPushButton('清除已截坐标')
        self.btn_reset_pending.setToolTip('等同按清除坐标键：清掉还没算成目标的坐标，上次算好的目标保留')
        self.btn_reset_pending.setEnabled(False)
        self.btn_reset_pending.clicked.connect(self.clear_pending)
        b6 = QPushButton('浮窗显隐')
        b6.clicked.connect(self.toggle_overlay)
        rowb2.addWidget(self.btn_reset_pending)
        rowb2.addWidget(b6)
        v.addLayout(rowb2)
        self.lb_tip = QLabel(self._tip_text())
        self.lb_tip.setStyleSheet('color:#6f8090;font-size:11px;')
        self.lb_tip.setWordWrap(True)
        v.addWidget(self.lb_tip)
        self.refresh_hotkey_texts()
        v.addStretch(1)
        scroll = QScrollArea()
        self.scroll = scroll
        scroll.setWidget(self.inner)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet('QScrollArea{background:transparent;border:0;}')
        outer = QVBoxLayout(self.panel)
        outer.setContentsMargins(4, 4, 4, 6)
        outer.setSpacing(6)
        outer.addWidget(scroll, 1)
        self.btn_quit = QPushButton('退出程序')
        self.btn_quit.setObjectName('danger')
        self.btn_quit.setMinimumHeight(36)
        self.btn_quit.setToolTip('关闭浮窗、注销全局热键并完全退出程序')
        self.btn_quit.clicked.connect(self.quit_app)
        outer.addWidget(self.btn_quit, 0)
        self._refresh_region_combo()
        self.refresh_region_label()
        self._refresh_faction_combo()
        self.refresh_targets_label()
        self._refresh_cmd_combos()
        _rsub = getattr(self, 'recon_sub', 'scope')
        self.btn_rsub_drone.setChecked(_rsub == 'drone')
        self.btn_rsub_scope.setChecked(_rsub == 'scope')
        self.btn_rsub_map.setChecked(_rsub == 'map')
        self.w_rreg_drone.setVisible(_rsub == 'drone')
        self.w_rreg_scope.setVisible(_rsub == 'scope')
        self.w_rreg_map.setVisible(_rsub == 'map')
        self._sync_rsrc_buttons()
        self._hk_suspended = False
        for _ed in self.qk_edits.values():
            _ed.installEventFilter(self)
        if getattr(self, 'ed_key', None) is not None:
            self.ed_key.installEventFilter(self)
            return None

    def eventFilter(self, obj, ev):
        if (isinstance(obj, QKeySequenceEdit) or obj is getattr(self, 'ed_key', None)):
            if ev.type() == QEvent.FocusIn:
                self._set_hotkeys_suspended(True)
            elif ev.type() == QEvent.FocusOut:
                QTimer.singleShot(0, (lambda: self._set_hotkeys_suspended(False)))
        return super().eventFilter(obj, ev)

    def _set_hotkeys_suspended(self, on):
        """on=True：注销全部全局热键，让输入框能收到任何键；on=False：重新注册。"""
        if on:
            if getattr(self, '_hk_suspended', False):
                return None
            self._hk_suspended = True
            hk = getattr(self, 'hotkeys', None)
            if hk is not None:
                try:
                    hk.stop()
                    hk.wait(800)
                except Exception:
                    pass
                self.hotkeys = None
            return None
        if not getattr(self, '_hk_suspended', False):
            return None
        eds = list(getattr(self, 'qk_edits', {}).values())
        if getattr(self, 'ed_key', None) is not None:
            eds.append(self.ed_key)
        for ed in eds:
            if not ed.hasFocus():
                continue
            return None
        self._hk_suspended = False
        self._start_hotkeys()

    @property
    def hk_text(self):
        """旧代码里的 self.hk_text 一律指「快速定位」键。"""
        return self.hk_texts[1]

    @property
    def tel_on(self):
        """旧代码兼容：望远镜子模式是否开启。"""
        return getattr(self, 'arty_sub', 'drone') == 'scope'

    def select_map(self, slug):
        for i, m in enumerate(self.maps):
            if not m.slug == slug:
                continue
            self.cb_map.setCurrentIndex(i)
            return None
        self.cb_map.setCurrentIndex(0)

    def select_weapon(self, key):
        i = self.cb_weap.findData(key)
        bad = i < 0
        self.cb_weap.setCurrentIndex(max(0, i))
        if not bad:
            return None
        # 配置里留着已删 / 没打进包的武器 key 时，不要无声地换成第一门炮：
        # 明确告一声（状态行放在 setCurrentIndex 之后——切武器会重算并改写状态行），
        # 同时把回退结果写进 run.log。
        fallback = self.weapons[0]['name'] if self.weapons else '—'
        print(f"[weapon] 配置里的武器 {key!r} 不在武器表里，回退到第一门：{fallback}", flush=True)
        if hasattr(self, 'lb_status'):
            self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
            self.lb_status.setText(f"武器「{key}」不在武器表里，已回退到「{fallback}」")
        return None

    @property
    def map(self):
        return self.maps[self.cb_map.currentIndex()]

    @property
    def weapon(self):
        return self.weapons[self.cb_weap.currentIndex()]

    def on_map_changed(self, _i):
        self.view.set_map(self.map)
        try:
            print(f"[map] {self.map.slug} · 底图 {self.map.base_px}×{self.map.base_px} ({'高清库' if self.map.hd else '旧底图'}) · 1 px ≈ {(1000.0 / max(self.map.px_per_m, 1e-06)):.0f} mm", flush=True)
        except Exception as e:
            print(f"[map] {self.map.slug} 底图信息读不到：{e}", flush=True)
        self.cfg['map'] = self.map.slug
        save_config(self.cfg)
        self.gun = None
        self.target = None
        self.sol = None
        self.los_res = None
        self.hgun = None
        self.gun_dz = 0.0
        self._gun_dz_key = None
        self._gun_dz_manual = None
        self.htg = None
        self.tgt_dz = 0.0
        self._tgt_dz_key = None
        self._tgt_dz_manual = None
        try:
            self.los_store.drop()
        except Exception:
            pass
        self.pend = []
        self.tgt_mode = None
        self._close_minimap()
        self.tel = None
        self.tel_warn = ''
        self._set_digit_keys(())
        self.view.player = None
        self.view.target_title = '目标'
        self.copied = ''
        self.recompute()

    def on_weapon_changed(self, _i):
        self.cfg['weapon'] = self.weapon['key']
        save_config(self.cfg)
        label = core.angle_label(self.weapon)
        if self.lb_angle is not None:
            self.lb_angle.setText(label)
        self.overlay.set_angle_label(label)
        self.recompute()

    def set_arc(self, key, save=True):
        """「间瞄火力支援」的弹道偏好：自动 / 低抛 / 高抛。
        只决定取哪一支解（低角 or 高角）；强制的那一支打不了就直接报错，
        不会偷偷换成另一端——点了低抛却拿到高抛的密位，比报个错危险得多。
        「呼叫弹幕」不受影响，仍按武器自己的习惯解算。"""
        key = key if key in core.ARC_MODES else 'auto'
        self.arc = key
        b = self.btns_arc.get(key)
        if b is not None and not b.isChecked():
            b.setChecked(True)
        self.cfg['arc'] = key
        if save:
            save_config(self.cfg)
        self.recompute()

    def on_gun_set(self, rx, rz):
        if rx < 0:
            self.gun = None
            self.tel = None
            self._set_digit_keys(())
            self.view.player = None
            self.recompute()
            return None
        self.gun = (rx, rz)
        if getattr(self, 'mode', 'arty') == 'barrage':
            uid = (self.cfg.get('bar_gun') or '').strip()
            if uid and self._bar_move_to(uid, rx, rz):
                return None
        self.recompute()

    def _bar_move_to(self, uid, rx, rz):
        lst = self.bar_positions()
        p = barrage.find(lst, uid)
        if not p:
            return False
        p['x'], p['z'] = float(rx), float(rz)
        self.cfg['bar_positions'] = lst
        save_config(self.cfg)
        self.refresh_bar_positions()
        self.view.gun = (float(rx), float(rz))
        self.view.update()
        self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
        self.lb_status.setText(f"阵地 {uid!s}（{(p.get('name') or '未命名')!s}）已挪到 {self.map.grid_at(rx, rz, 4) if self.map else '—'!s}")
        self.bar_recompute()
        return True

    def _commit_target(self, target, mode):
        """确定一个目标坐标：刷新面板/地图，并记住它作为「上一次算好的目标」。"""
        self.target = (max(0.0, min(target[0], self.map.spanx)), max(0.0, min(target[1], self.map.spanz)))
        self.tgt_mode = mode
        self.last_target = (self.target, mode)
        self.view.target = self.target
        self.view.update()
        self.copied = ''

    def grid_level(self):
        """当前该显示几级坐标：侦察报告、望远镜火力支援用 4 级，其余间瞄都用 3 级。"""
        if getattr(self, 'mode', 'arty') == 'recon':
            return 4
        if self.tgt_mode == 'tel':
            return 4
        return 3

    def _copy_solution(self, sol, res):
        """算完密位/方位后，把「密位 … 方位 …」直接放进剪贴板（游戏聊天里粘贴）。
    角度单位跟着武器走：mil 的炮复制「密位 1326.42 方位 122.0°」，
    deg / degMin 的（火箭巢、车载炮）复制「射角 12.34° 方位 122.0°」。
    1.11.0 起角度固定 2 位小数（方位仍是 1 位）。"""
        self.copied = ''
        if not sol or not res:
            return None
        w = self.weapon
        brg = f"{res['brg']:.1f}°"
        if w.get('unit') == 'mil':
            txt = f"密位 {sol['mil']:.2f} 方位 {brg}"
        else:
            txt = f"射角 {core.angle_text(w, sol['deg'])} 方位 {brg}"
        if set_clipboard_text(txt):
            self.copied = txt
            return None

    def set_target_grid(self, text):
        try:
            rx, rz = core.parse(text)
        except core.ParseError as e:
            self.lb_status.setText(str(e))
            return False
        else:
            if rx > (self.map.spanx + 1) or rz > (self.map.spanz + 1):
                self.lb_status.setText(f"{text} 不在这张地图上")
                return False
            self.pend = []
            self._commit_target((rx, rz), 'grid')
            self.recompute()
            return True

    def _dz_paint(self, spin, lab, dz, auto, label, manual, hint=''):
        """把某一行的抬升同步到输入框与提示（回填时不触发 valueChanged）。"""
        self._dz_lock = True
        try:
            if spin is not None:
                spin.setValue(round(float(dz), 1))
        finally:
            self._dz_lock = False
        if lab is None:
            return None
        if manual is not None:
            lab.setText(f"手动 · 自动{('+' + format(auto, '.1f') + ' m') if auto >= 0.05 else '地形'}")
            lab.setStyleSheet('color:#ffd166;font-size:11px;')
        elif auto >= 0.05:
            lab.setText(f"自动 · {label or '结构'} +{auto:.1f} m")
            lab.setStyleSheet('color:#ffd166;font-size:11px;')
        elif hint:
            lab.setText(f"地形（脚下{hint}，站上去请手填）")
            lab.setStyleSheet('color:#ffd166;font-size:11px;')
        else:
            lab.setText('自动 · 地形')
            lab.setStyleSheet('color:#7f909f;font-size:11px;')

    def _dz_ui(self, dz, auto, label, manual, hint=''):
        """阵地那一行。"""
        self._dz_paint(getattr(self, 'spin_dz', None), getattr(self, 'lb_dz', None),
                       dz, auto, label, manual, hint)

    def _tdz_ui(self, dz, auto, label, manual, hint=''):
        """目标那一行。"""
        self._dz_paint(getattr(self, 'spin_tdz', None), getattr(self, 'lb_tdz', None),
                       dz, auto, label, manual, hint)

    def _lift_info(self, pt):
        """某点脚下的结构抬升：(auto, label, hint)。

        自动判定用 los.support_at（只认桥面 / 集装箱 / 货箱的盒顶）；
        没自动抬但脚下压着楼体时给个提示（整栋楼的盒子不能当站立面，让人自己填）。
        """
        auto, label, hint = 0.0, '', ''
        try:
            lm = self.los_store.get(self.map)
            auto, label = los.support_at(self.map, lm, pt[0], pt[1])
            if auto < 0.05:
                h, hl = los.roof_hint(self.map, lm, pt[0], pt[1])
                if h >= 1.0:
                    hint = f"{hl} {h:.0f} m"
        except Exception as e:
            print(f"[los] 高度检测失败：{e}", flush=True)
        return auto, label, hint

    def _gun_height(self):
        """炮位实际高度 = 地形 + 抬升（桥面 / 集装箱…）。

        自动判定用 los.support_at（盒顶相对地形），手动值优先于自动；换阵地自动失效。
        """
        gnd = self.map.h_at(*self.gun)
        key = (getattr(self.map, 'slug', ''), round(float(self.gun[0]), 1),
               round(float(self.gun[1]), 1))
        if key != self._gun_dz_key:
            self._gun_dz_key = key
            self._gun_dz_manual = None      # 换了阵地，手动值不再适用
        auto, label, hint = self._lift_info(self.gun)
        self.gun_dz_auto, self.gun_dz_label = auto, label
        dz = auto if self._gun_dz_manual is None else float(self._gun_dz_manual)
        self.gun_dz = dz
        self._dz_ui(dz, auto, label, self._gun_dz_manual, hint)
        self.hgun = gnd + dz
        return self.hgun

    def _tgt_height(self):
        """目标实际高度 = 地形 + 抬升。规则和炮位一样：自动只认桥/集装箱/货箱，换目标就退回自动。"""
        gnd = self.map.h_at(*self.target)
        key = (getattr(self.map, 'slug', ''), round(float(self.target[0]), 1),
               round(float(self.target[1]), 1))
        if key != self._tgt_dz_key:
            self._tgt_dz_key = key
            self._tgt_dz_manual = None
        auto, label, hint = self._lift_info(self.target)
        self.tgt_dz_auto, self.tgt_dz_label = auto, label
        dz = auto if self._tgt_dz_manual is None else float(self._tgt_dz_manual)
        self.tgt_dz = dz
        self._tdz_ui(dz, auto, label, self._tgt_dz_manual, hint)
        self.htg = gnd + dz
        return self.htg

    def on_gun_dz_changed(self, val):
        """手动改「阵地高出地面」：以输入为准，立刻重算。"""
        if self._dz_lock:
            return None
        self._gun_dz_manual = round(float(val), 1)
        self.recompute()
        return None

    def gun_dz_reset(self):
        """「自动」按钮：丢掉手动值，回到按桥面/集装箱自动判定。"""
        self._gun_dz_manual = None
        self.recompute()
        return None

    def on_tgt_dz_changed(self, val):
        """手动改「目标高出地面」：以输入为准，立刻重算。"""
        if self._dz_lock:
            return None
        self._tgt_dz_manual = round(float(val), 1)
        self.recompute()
        return None

    def tgt_dz_reset(self):
        """「自动」按钮：丢掉目标的手动值。"""
        self._tgt_dz_manual = None
        self.recompute()
        return None

    def recompute(self):
        if getattr(self, 'mode', 'arty') == 'recon':
            self.recon_recompute()
            return None
        if getattr(self, 'mode', 'arty') == 'barrage':
            self.bar_recompute()
            return None
        self.view.grid_level = self.grid_level()
        w = self.weapon
        self.overlay.set_angle_label(core.angle_label(w))
        if hasattr(self, 'btn_reset_pending'):
            self.btn_reset_pending.setEnabled(bool(self.pend))
        if not self.gun:
            self.los_res = None
            self.hgun = None
            self._refresh_minimap_spread()
            self._dz_ui(0.0, 0.0, '', None)
            self._tdz_ui(0.0, 0.0, '', None)
            self.htg = None
            self.copied = ''
            self.vals['gun'].setText('—')
            self.vals['tgt'].setText('—')
            self.vals['brg'].setText('—')
            self.vals['mil'].setText('—')
            self.lb_extra.setText('')
            self.lb_status.setText('① 在地图上点出阵地（右键可清除）')
            self.overlay.set_values('—', '—', '—', '—', '先在地图上点出阵地')
            self.overlay.set_status('idle', '待命 · 先设阵地')
            return None
        hgun = self._gun_height()
        gun_grid = self.map.grid_at(*self.gun, 3)
        self.vals['gun'].setText(gun_grid)
        gun_txt = f"{gun_grid} · {hgun:.0f} m"
        if self.gun_dz >= 0.05:
            gun_txt += f" · +{self.gun_dz:.1f} m {self.gun_dz_label or '抬升'}"
        if self.tel and self.tel.get('state') not in (None, 'done'):
            self.tel_prompt(gun_txt)
            return None
        if not self.target:
            self.los_res = None
            self._refresh_minimap_spread()
            self._tdz_ui(0.0, 0.0, '', None)
            self.htg = None
            self.copied = ''
            self.vals['tgt'].setText('—')
            self.vals['brg'].setText('—')
            self.vals['mil'].setText('—')
            self.lb_extra.setText('')
            if self.pend:
                got = [self.map.grid_at(q[0], q[1], 3) for q in self.pend]
                k = len(self.pend)
                marks = '①②③'[:k]
                shown = ' + '.join(got)
                self.vals['tgt'].setText(f"{shown} {marks}")
                self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
                self.lb_status.setText(f"已记 {k} 个（{shown}）——把准星再挪一点，按 {self.hk_texts[2]} 截第 {(k + 1)} 个（共 {PEND_TOTAL} 次，取三点外心）")
                self.overlay.set_values(gun_txt, f"{marks[-1]} {got[-1]}（{k}/{PEND_TOTAL}）", '—', '—', f"已记 {shown} · 再按 {self.hk_texts[2]} 截第 {(k + 1)} 个")
                self.overlay.set_status('idle', f"{marks} 已记 {got[-1]} · 等第 {(k + 1)} 个（{(k + 1)}/{PEND_TOTAL}）")
                return None
            self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
            self.lb_status.setText(f"② 切回游戏：按 {self.hk_text} 快速定位（截 1 次出结果），或按 {self.hk_texts[2]} 准确定位（截 {PEND_TOTAL} 次取三点外心）")
            self.overlay.set_values(gun_txt, '—', '—', '—', f"{self.hk_text} 快速定位（1 次）· {self.hk_texts[2]} 准确定位（{PEND_TOTAL} 次）")
            self.overlay.set_status('idle', f"待命 · {self.hk_text} 快速 / {self.hk_texts[2]} 准确")
            return None
        htg = self._tgt_height()
        res = core.evaluate(w, self.gun, self.target, hgun, htg, self.arc)
        self.sol = res
        self._los_update(res, hgun)
        self._refresh_minimap_spread()
        sol = res.get('sol')
        tgt_grid = self.map.grid_at(*self.target, self.grid_level())
        tgt_txt = f"{tgt_grid} · {htg:.0f} m"
        if self.tgt_dz >= 0.05:
            tgt_txt += f" · +{self.tgt_dz:.1f} m {self.tgt_dz_label or '抬升'}"
        self.vals['tgt'].setText(tgt_grid)
        self.vals['brg'].setText(f"{res['brg']:.1f}°")
        if sol:
            unit = core.angle_text(w, sol['deg'])
            self.vals['mil'].setText(core.angle_display(w, sol))
            self._copy_solution(sol, res)
            self.lb_extra.setText(f"距离 {res['dist']:.0f} m · 高差 {res['dh']:+.0f} m · {res['solName']} · 飞行 {sol['tof']:.1f} s · 散布 {sol['sprA']:.0f}×{sol['sprB']:.0f} m")
            note = f"飞行 {sol['tof']:.1f} s · 距离 {res['dist']:.0f} m" if w.get('unit') == 'mil' else f"射角 {unit} · 飞行 {sol['tof']:.1f} s"
            los_note = self._los_note()
            if los_note:
                self.lb_extra.setText((self.lb_extra.text() + ' · ' + los_note))
                note += ' · ' + los_note
            if res.get('warnMin'):
                note += ' · 小于最小射程'
            mode_txt = MODE_TEXT.get(self.tgt_mode, '')
            if mode_txt:
                note += f" · {mode_txt}"
                self.lb_extra.setText((self.lb_extra.text() + f" · {mode_txt}"))
            if self.copied:
                self.lb_extra.setText((self.lb_extra.text() + ' · 已复制'))
            danger = self.tel_danger(res)
            self.tel_warn = danger
            if danger:
                note += ' · ' + danger
                self.lb_extra.setText(((self.lb_extra.text() + ' · ') + danger))
            self.overlay.set_values(gun_txt, tgt_txt, f"{res['brg']:.1f}°", core.angle_display(w, sol), note)
            self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
            self.lb_status.setText(f"{res['solName']}{core.angle_label(w)} {unit} · 距离 {res['dist']:.0f} m · 方位 {res['brg']:.1f}°")
            if los_note:
                self.lb_status.setStyleSheet('color:#ffb86b;font-size:12px;')
                self.lb_status.setText((self.lb_status.text() + ' · ⚠弹道被挡'))
            self.overlay.set_status('done', (((f"计算完成 · {tgt_grid}" + (' · 已复制' if self.copied else ''))) + (' · ⚠散布圈内' if self.tel_warn else '')) + (' · ⚠弹道被挡' if los_note else ''))
            if self.tel_warn:
                self.lb_status.setText((self.lb_status.text() + f" · {self.tel_warn}"))
                return None
            return None
        why = (res.get('err') or '无有效解')
        self.los_res = None
        self.copied = ''
        self.vals['mil'].setText('—')
        self.lb_extra.setText(f"距离 {res['dist']:.0f} m · 高差 {res['dh']:+.0f} m")
        self.overlay.set_values(gun_txt, tgt_txt, f"{res['brg']:.1f}°", '不可达', (res.get('errShort') or why[:22]), bad=True)
        self.lb_status.setStyleSheet('color:#ff8f8f;font-size:12px;')
        self.lb_status.setText(f"打不到：{why}（有效射程 {self.range_text(w)}）")
        self.overlay.set_status('error', f"计算失败 · {why}"[:28])

    def range_text(self, w):
        lo, hi = None, None
        d = max(4.0, ((w.get('minD') or 0) - 40))
        while d <= 7000:
            s = core.solve(w, d, 0.0)
            good = any(((s[k] and s[k]['ok']) for k in ('low', 'high')))
            if good:
                lo = d if lo is None else lo
                hi = d
            d += 20
        if lo is None:
            return '—'
        return f"{lo:.0f}–{hi:.0f} m"

    SUB_KEYS = {'drone': ('regions', 'region_name', 'region', 'region_az'), 'scope': ('s_regions', 's_region_name', 's_region', 's_region_az')}
    def _sub_keys(self, sub=None):
        return self.SUB_KEYS['scope' if (sub or self.arty_sub) == 'scope' else 'drone']

    def _region_combo(self, sub=None):
        if (sub or self.arty_sub) == 'scope':
            return self.cb_region_s
        return self.cb_region

    def _regions(self, sub=None):
        rs = self.cfg.get(self._sub_keys(sub)[0])
        if isinstance(rs, list):
            return rs
        return []

    def _region_names(self, sub=None):
        return [r.get('name', '') for r in self._regions(sub)]

    def _refresh_region_combo(self, select=None, sub=None):
        sub = 'scope' if (sub or self.arty_sub) == 'scope' else 'drone'
        keys = self._sub_keys(sub)
        combo = self._region_combo(sub)
        cur = (self.cfg.get(keys[1]) or '')
        names = self._region_names(sub)
        combo.blockSignals(True)
        combo.clear()
        for n in names:
            combo.addItem(n, n)
        target = select if select is not None else cur
        combo.setCurrentIndex(names.index(target) if target in names else -1)
        combo.blockSignals(False)

    def _refresh_all_region_combos(self):
        self._refresh_region_combo(None, 'drone')
        self._refresh_region_combo(None, 'scope')

    def on_region_preset(self, idx, sub=None):
        sub = 'scope' if (sub or self.arty_sub) == 'scope' else 'drone'
        keys = self._sub_keys(sub)
        combo = self._region_combo(sub)
        if idx < 0:
            return None
        name = combo.itemData(idx)
        tag = '望远镜' if sub == 'scope' else '无人机'
        for r in self._regions(sub):
            if not r.get('name') == name:
                continue
            if r.get('rect'):
                self.cfg[keys[2]] = list(r['rect'])
            if r.get('rect_az'):
                self.cfg[keys[3]] = list(r['rect_az'])
            if sub == 'drone':
                if r.get('rect_az_cmd'):
                    self.cfg['d_region_az_cmd'] = list(r['rect_az_cmd'])
                if r.get('rect_az_scout'):
                    self.cfg['d_region_az_scout'] = list(r['rect_az_scout'])
                self.drone = None
            self.cfg[keys[1]] = name
            save_config(self.cfg)
            self.refresh_region_label()
            parts = []
            if r.get('rect'):
                parts.append(f"坐标 {r['rect'][2]} × {r['rect'][3]} px")
            if r.get('rect_az'):
                parts.append(f"方位角 {r['rect_az'][2]} × {r['rect_az'][3]} px")
            if hasattr(self, 'lb_scope'):
                self.lb_scope.setText(self._scope_hint())
            if hasattr(self, 'lb_drone_hint'):
                self.lb_drone_hint.setText(self._drone_hint())
            self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
            self.lb_status.setText(f"已套用{tag}预设「{name}」（{(' + '.join(parts) or '空')}）")
            return None

    def save_region_preset(self, sub=None):
        sub = 'scope' if (sub or self.arty_sub) == 'scope' else 'drone'
        keys = self._sub_keys(sub)
        tag = '望远镜' if sub == 'scope' else '无人机'
        cur = (self.cfg.get(keys[1]) or '')
        default = (cur or f"区域 {(len(self._regions(sub)) + 1)}")
        name, ok = QInputDialog.getText(self, f"保存{tag}截图区域", '给这个框选区域起个名字（同名会覆盖）：', QLineEdit.Normal, default)
        if not ok:
            return None
        name = (name or '').strip()
        if not name:
            QMessageBox.information(self, '保存截图区域', '名字不能为空。')
            return None
        rect = list(self.cfg[keys[2]]) if self.cfg.get(keys[2]) else None
        az = list(self.cfg[keys[3]]) if self.cfg.get(keys[3]) else None
        if sub == 'drone':
            pass
        azc = list(self.cfg['d_region_az_cmd']) if self.cfg.get('d_region_az_cmd') else None
        if sub == 'drone':
            pass
        azs = list(self.cfg['d_region_az_scout']) if self.cfg.get('d_region_az_scout') else None
        if not rect and not az:
            QMessageBox.information(self, '保存截图区域', '两块都还没框：先点「框选玩家坐标」或「框选方位角」拖一个框。')
            return None
        rs = self._regions(sub)
        for r in rs:
            if not r.get('name') == name:
                continue
            r['rect'] = rect
            r['rect_az'] = az
            r['rect_az_cmd'] = azc
            r['rect_az_scout'] = azs
            break
        else:
            rs.append({'name': name, 'rect': rect, 'rect_az': az, 'rect_az_cmd': azc, 'rect_az_scout': azs})
        self.cfg[keys[0]] = rs
        self.cfg[keys[1]] = name
        save_config(self.cfg)
        self._refresh_region_combo(name, sub)
        self.refresh_region_label()
        parts = []
        if rect:
            parts.append(f"坐标 {rect[2]} × {rect[3]} px")
        if az:
            parts.append(f"方位角 {az[2]} × {az[3]} px")
        self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
        self.lb_status.setText(f"已保存{tag}预设「{name}」（{' + '.join(parts)}）")

    def rename_region_preset(self, sub=None):
        sub = 'scope' if (sub or self.arty_sub) == 'scope' else 'drone'
        keys = self._sub_keys(sub)
        combo = self._region_combo(sub)
        idx = combo.currentIndex()
        if idx < 0:
            QMessageBox.information(self, '重命名区域', '先在上面的下拉里选一个已保存的区域。')
            return None
        old = combo.itemData(idx)
        self._set_hotkeys_suspended(True)
        try:
            new, ok = QInputDialog.getText(self, '重命名区域', f"把「{old}」改成：", QLineEdit.Normal, old)
        finally:
            self._set_hotkeys_suspended(False)
        if not ok:
            return None
        new = (new or '').strip()
        if not new or new == old:
            return None
        if new in self._region_names(sub):
            QMessageBox.information(self, '重命名区域', f"已经有叫「{new}」的区域了，换一个名字。")
            return None
        for r in self._regions(sub):
            if not r.get('name') == old:
                continue
            r['name'] = new
            break
        if (self.cfg.get(keys[1]) or '') == old:
            self.cfg[keys[1]] = new
        save_config(self.cfg)
        self._refresh_region_combo(new, sub)
        self.refresh_region_label()
        self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
        self.lb_status.setText(f"已重命名：「{old}」→「{new}」")

    def delete_region_preset(self, sub=None):
        sub = 'scope' if (sub or self.arty_sub) == 'scope' else 'drone'
        keys = self._sub_keys(sub)
        combo = self._region_combo(sub)
        idx = combo.currentIndex()
        if idx < 0:
            QMessageBox.information(self, '删除区域', '先在上面的下拉里选一个已保存的区域。')
            return None
        name = combo.itemData(idx)
        if QMessageBox.question(self, '删除区域', f"删掉预设「{name}」？") != QMessageBox.Yes:
            return None
        self.cfg[keys[0]] = [r for r in self._regions(sub) if r.get('name') != name]
        if (self.cfg.get(keys[1]) or '') == name:
            self.cfg[keys[1]] = ''
        save_config(self.cfg)
        self._refresh_region_combo(None, sub)
        self.refresh_region_label()
        self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
        self.lb_status.setText(f"已删除预设「{name}」")

    def refresh_region_label(self):
        r = self.cfg.get('region')
        if r:
            name = (self.cfg.get('region_name') or '')
            tail = f" · 预设「{name}」" if name else ' · 未保存'
            self.lb_region.setText(f"{r[2]} × {r[3]} px  @ ({r[0]}, {r[1]}){tail}")
        else:
            self.lb_region.setText('未设 · 点「框选坐标区域」框一下')
        if hasattr(self, 'lb_s_region'):
            sr = self.cfg.get('s_region')
            if sr:
                name = (self.cfg.get('s_region_name') or '')
                tail = f" · 预设「{name}」" if name else ' · 未保存'
                self.lb_s_region.setText(f"{sr[2]} × {sr[3]} px  @ ({sr[0]}, {sr[1]}){tail}")
            else:
                self.lb_s_region.setText('未设 · 点「框选玩家坐标」框一下')
            sa = self.cfg.get('s_region_az')
            if sa:
                self.lb_s_az.setText(f"{sa[2]} × {sa[3]} px  @ ({sa[0]}, {sa[1]})")
            else:
                self.lb_s_az.setText('未设 · 点「框选方位角」框一下（望远镜第 1 步要读它）')
        if hasattr(self, 'lb_recon_az'):
            ra = self.cfg.get('r_region_az')
            if ra:
                self.lb_recon_az.setText(f"{ra[2]} × {ra[3]} px  @ ({ra[0]}, {ra[1]})")
            else:
                self.lb_recon_az.setText('未单独设 · 点「框选侦察方位角」框一下，侦察流程才能读方位')
            rr = self.cfg.get('r_region')
            if rr:
                self.lb_recon_region.setText(f"{rr[2]} × {rr[3]} px  @ ({rr[0]}, {rr[1]})")
            else:
                self.lb_recon_region.setText('未单独设 · 点「框选侦察坐标」框一下')
        if hasattr(self, 'lb_d_az_cmd'):
            ca = self.cfg.get('d_region_az_cmd')
            sa = self.cfg.get('d_region_az_scout')
            if ca:
                self.lb_d_az_cmd.setText(self._rect_text(ca))
            else:
                self.lb_d_az_cmd.setText('未设 · 点「框选指挥官无人机方位角」')
            if sa:
                self.lb_d_az_scout.setText(self._rect_text(sa))
            else:
                self.lb_d_az_scout.setText('未设 · 点「框选侦察兵无人机方位角」')
        if hasattr(self, 'lb_recon_tgt'):
            src = self._src_name(getattr(self, 'recon_src', 'cmd'))
            rt = self.cfg.get(self._tgt_key())
            if rt:
                self.lb_recon_tgt.setText(f"{src} · {rt[2]} × {rt[3]} px  @ ({rt[0]}, {rt[1]})")
            else:
                self.lb_recon_tgt.setText(f"{src} · 未设 · 点「框选目标坐标」框一下")
        if hasattr(self, 'lb_recon_az_d'):
            who = 'scout' if getattr(self, 'recon_src', 'cmd') == 'scout' else 'cmd'
            da = self.cfg.get(self.drone_az_key(who))
            if da:
                self.lb_recon_az_d.setText(f"{self._src_name(who)} · {self._rect_text(da)}")
                return None
            self.lb_recon_az_d.setText(f"{self._src_name(who)} · 未设 —— 不框就退回一次读完（直接读目标坐标、不弹小地图）")
            return None

    def pick_recon_drone_az(self):
        """框「当前这台无人机」的方位角：跟间瞄·无人机视角共用同一块区域。"""
        who = 'scout' if getattr(self, 'recon_src', 'cmd') == 'scout' else 'cmd'
        if who == 'scout':
            self.pick_region('d_az_scout')
            return None
        self.pick_region('d_az_cmd')

    def preview_recon_drone_az(self):
        who = 'scout' if getattr(self, 'recon_src', 'cmd') == 'scout' else 'cmd'
        if who == 'scout':
            self.preview_region('d_az_scout')
            return None
        self.preview_region('d_az_cmd')

    def pick_region(self, which='grid'):
        self._pick_which = which
        self._ov_was_visible = self.overlay.isVisible()
        self.overlay.hide()
        self.hide()
        self.sel = RegionSelector(which)
        self.sel.picked.connect(self.on_region_picked)
        self.sel.cancelled.connect(self.on_region_cancelled)
        self.sel.show()
        self.sel.raise_()
        self.sel.activateWindow()

    def _after_pick(self, msg):
        """框选结束（成功或取消）后一定把主窗口与浮窗还回来。"""
        self.show()
        self.activateWindow()
        if getattr(self, '_ov_was_visible', True):
            self.overlay.show()
        self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
        self.lb_status.setText(msg)

    def on_region_cancelled(self):
        self._after_pick('已取消框选（原来的区域没变）')

    def on_region_picked(self, rect):
        which = getattr(self, '_pick_which', 'grid')
        rect = [int(v) for v in rect]
        pos = f"{rect[2]} × {rect[3]} px @ ({rect[0]}, {rect[1]})"
        if which in ('s_az', 'az'):
            self.cfg['s_region_az'] = rect
            msg = f"望远镜方位角区域已更新：{pos}"
        elif which in ('d_az_cmd', 'd_az_scout'):
            who = 'scout' if which == 'd_az_scout' else 'cmd'
            self.cfg[self.drone_az_key(who)] = rect
            self.drone = None
            msg = f"{self.DRONE_WHO_TEXT[who]}方位角区域已更新：{pos}"
        elif which == 's_grid':
            self.cfg['s_region'] = rect
            msg = f"望远镜玩家坐标区域已更新：{pos}"
        elif which == 'r_az':
            self.cfg['r_region_az'] = rect
            msg = f"侦察方位角区域已更新：{pos}"
        elif which == 'r_grid':
            self.cfg['r_region'] = rect
            msg = f"侦察坐标区域已更新：{pos}"
        elif which == 'r_tgt':
            src = self._src_name(getattr(self, 'recon_src', 'cmd'))
            self.cfg[self._tgt_key()] = rect
            msg = f"{src}的敌目标坐标区域已更新：{pos}"
        else:
            self.cfg['region'] = rect
            msg = f"无人机坐标区域已更新：{pos}"
        save_config(self.cfg)
        self.refresh_region_label()
        self._after_pick(msg)
        if which in ('r_az', 'r_grid', 'r_tgt'):
            try:
                self.recon_recompute()
            except Exception:
                pass
            self.lb_status.setText(msg)
        if which in ('s_az', 's_grid') and hasattr(self, 'lb_scope'):
            self.lb_scope.setText(self._scope_hint())
        if which in ('d_az_cmd', 'd_az_scout') and hasattr(self, 'lb_drone_hint'):
            self.lb_drone_hint.setText(self._drone_hint())
        self.preview_region(which)

    def _region_of(self, which='grid'):
        """which 可以是 grid / s_grid / s_az / r_grid / r_az，返回 [x, y, w, h] 或 None。"""
        if which in ('s_az', 'az'):
            return self.az_region()
        if which == 's_grid':
            return self.scope_grid_region()
        if which == 'r_az':
            return self.recon_az_region()
        if which == 'r_grid':
            return self.recon_grid_region()
        if which == 'r_tgt':
            return self.recon_tgt_region()
        if which in ('d_az_cmd', 'd_az_scout'):
            return self.drone_az_region('scout' if which == 'd_az_scout' else 'cmd')
        r = self.cfg.get('region')
        if r:
            return list(r)

    def preview_region(self, which='grid'):
        region = self._region_of(which)
        if not region:
            self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
            self.lb_status.setText('这块区域还没框过：先点对应的「框选…」按钮拖一个框')
            return None
        try:
            img = vision.grab(tuple(region))
        except Exception as e:
            QMessageBox.warning(self, '截图失败', str(e))
            return None
        else:
            preview = img.copy()
            preview.thumbnail((360, 200))
            pm = QPixmap.fromImage(QImage(preview.tobytes(), preview.width, preview.height, QImage.Format_RGB888))
            dlg = QMessageBox(self)
            dlg.setWindowTitle('识别区域预览')
            dlg.setIconPixmap(pm)
            if which == 'r_tgt':
                title = f"敌目标坐标区域（{self._src_name(getattr(self, 'recon_src', 'cmd'))}）"
            else:
                title = {'grid': '无人机坐标区域', 'az': '望远镜方位角区域', 's_grid': '望远镜玩家坐标区域', 's_az': '望远镜方位角区域', 'r_grid': '侦察坐标区域', 'r_az': '侦察方位角区域', 'r_tgt': '敌目标坐标区域（无人机视角）', 'd_az_cmd': '指挥官无人机方位角区域', 'd_az_scout': '侦察兵无人机方位角区域'}.get(which, '识别区域')
            dlg.setText(f"{title} {region}")
            dlg.exec()

    def test_ocr_region(self):
        self.start_ocr()

    DRONE_WHO_TEXT = {'cmd': '指挥官无人机', 'scout': '侦察兵无人机'}
    DRONE_SPAN = 60.0
    def drone_who(self):
        if self.cfg.get('drone_who') == 'scout':
            return 'scout'
        return 'cmd'

    def drone_az_key(self, who=None):
        """两台无人机的方位角框分开存：指挥官 d_region_az_cmd / 侦察兵 d_region_az_scout。"""
        w = who if who in ('cmd', 'scout') else self.drone_who()
        if w == 'scout':
            return 'd_region_az_scout'
        return 'd_region_az_cmd'

    def drone_az_region(self, who=None):
        r = self.cfg.get(self.drone_az_key(who))
        if r:
            return list(r)

    def _drone_has_az(self):
        return bool((self.drone and self.drone.get('az') is not None))

    def _drone_target_first(self):
        """快速定位的两步里，先读目标坐标、后读方位角吗？
    指挥官无人机一直在盘旋，方位角时刻在变 —— 先读坐标再读方位角，小地图用的就是刚读到的
    最新方位；侦察兵无人机不盘旋，仍按老顺序（先方位角、后坐标）。"""
        return (self.drone_who() == 'cmd' and bool(self.drone_az_region()))

    @staticmethod
    def _rect_text(r):
        if r:
            return f"{r[2]} × {r[3]} px  @ ({r[0]}, {r[1]})"
        return '未设'

    def _drone_hint(self):
        """无人机快速定位的步骤说明（面板里那行灰字）。"""
        hk = self.hk_texts.get(1, 'F1')
        who = self.DRONE_WHO_TEXT[self.drone_who()]
        got = self.drone_az_region()
        if not got:
            return f"还缺：{who}的方位角区域（在上面框一下）。框好后：按 {hk} 读目标坐标 → 自动弹 60 × 60 m 小地图，点出目标真实位置。（没框方位角框就只读目标坐标、不弹小地图。）"
        if self._drone_target_first():
            return f"{who}方位角区域就绪。按 {hk}：① 读目标坐标 → ② 读方位角 → 弹 60 × 60 m 小地图（上方=刚读到的方位角），点一下目标真实位置即可；点不到按 ⌫ 重截目标坐标。"
        return f"{who}方位角区域就绪。按 {hk}：① 读方位角 → ② 读目标坐标 → 弹 60 × 60 m 小地图（上方=方位角），点一下目标真实位置即可；点不到按 ⌫ 重截目标坐标。"

    def set_drone_who(self, who):
        """切换「现在看的是哪台无人机」：快速定位就按这台的位置框去读方位角。"""
        who = 'scout' if who == 'scout' else 'cmd'
        if self.cfg.get('drone_who') == who:
            return None
        self.cfg['drone_who'] = who
        save_config(self.cfg)
        self.drone = None
        self._close_minimap()
        if hasattr(self, 'lb_drone_hint'):
            self.lb_drone_hint.setText(self._drone_hint())
        self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
        self.lb_status.setText(f"已切到「{self.DRONE_WHO_TEXT[who]}」：快速定位会读它的方位角")

    def drone_reset(self, msg=''):
        """清掉无人机快速定位没走完的状态（上次算好的目标/阵地都不动）。"""
        self.drone = None
        if msg:
            self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
            self.lb_status.setText(msg)
            self.recompute()
            return None

    def on_drone_az_ok(self, coord, raw, ms):
        """快速定位的方位角读到了：指挥官无人机是第 2 步（坐标已读，接着就弹图），
    侦察兵无人机是第 1 步（还得再截一次目标坐标）。"""
        who = self.DRONE_WHO_TEXT[self.drone_who()]
        hk = self.hk_texts.get(1, 'F1')
        dr = self.drone or {}
        keep_tgt = (dr.get('state') == 'tgt_az' and bool(dr.get('tgt')))
        if not coord:
            if not keep_tgt:
                self.drone = None
            self.lb_status.setStyleSheet('color:#ff8f8f;font-size:12px;')
            self.lb_status.setText((f"没读到{who}的方位角（模型说：{raw[:40]}）—— 再按 {hk} " + ('重读方位角（坐标已记）' if keep_tgt else '重试')))
            self.overlay.set_status('error', '没读到方位角 · 再截一次')
            self.overlay.set_values(self.overlay.rows[0][1], '—', '—', '—', '方位角那格里没看到数字', bad=True)
            return None
        try:
            az = (float(str(coord).strip().rstrip('°')) % 360.0)
        except (TypeError, ValueError):
            if not keep_tgt:
                self.drone = None
            self.lb_status.setStyleSheet('color:#ff8f8f;font-size:12px;')
            self.lb_status.setText((f"方位角读出来不是数字（{coord}）—— 再按 {hk} " + ('重读方位角（坐标已记）' if keep_tgt else '重试')))
            self.overlay.set_status('error', '方位角不是数字 · 再截一次')
            return None
        else:
            if keep_tgt:
                dr['az'] = az
                if not dr.get('coord'):
                    dr.get('coord')
                self._drone_show_minimap(dr['tgt'], '', az, self._ocr_src(raw, ms))
                return None
            self.drone = {'az': az, 'state': 'pos'}
            self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
            self.lb_status.setText(f"{who}方位角 {az:.0f}° · {self._ocr_src(raw, ms)} —— 再按 {hk} 截目标坐标（快速定位 2/2），读完会弹 {self.DRONE_SPAN:g} m 小地图")
            self.overlay.set_status('idle', f"方位 {az:.0f}° 已记 · 再按 {hk} 截目标坐标")
            self.overlay.set_values(self.overlay.rows[0][1], '—', '—', '—', f"方位 {az:.0f}° 已记 · 等第 2 步目标坐标")

    def _drone_show_minimap(self, pt, coord, az, src_txt):
        """坐标与方位角都齐了：把 pt 当目标、弹 60 m 小地图等玩家点真实位置。"""
        self.pend = []
        self._commit_target(pt, 'single1')
        self.recompute()
        grid = self.map.grid_at(*self.target, self.grid_level())
        hint = '（I 易被误读成 L，请核对）' if (coord or '').startswith('I') else ''
        if self.drone:
            self.drone['state'] = 'done'
        if self._open_minimap(self.target, az, None, span=self.DRONE_SPAN):
            self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
            self.lb_status.setText(f"快速定位：{coord}{hint} · {src_txt} · 目标 {grid}（估测） —— 小地图已弹出：点一下目标的真实位置；找不到就按 ⌫ 重截目标坐标")
            self.overlay.set_status('done', f"点小地图修正目标 · {grid}")
            self.overlay.set_values(self.overlay.rows[0][1], grid, '—', '—', '点小地图上目标的真实位置 · ⌫ 重截坐标')
            return None
        self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
        self.lb_status.setText(f"快速定位：{coord}{hint} · {src_txt}（底图没加载，小地图没弹出来）")

    def on_drone_pos_ok(self, coord, raw, ms):
        """快速定位的目标坐标读到了。指挥官无人机：先记下坐标，等第 2 步读方位角；
    侦察兵无人机：这就是第 2 步（方位角早读过）→ 直接弹 60 m 小地图。"""
        hk = self.hk_texts.get(1, 'F1')
        dr = self.drone or {}
        wait_state = 'tgt' if self._drone_target_first() else 'pos'
        if not coord:
            if self.drone:
                self.drone['state'] = wait_state
            self.lb_status.setStyleSheet('color:#ff8f8f;font-size:12px;')
            self.lb_status.setText((f"没读到目标坐标（模型说：{raw[:40]}）—— 再按 {hk} 重试" + ('（方位角已记）' if wait_state == 'pos' else '')))
            self.overlay.set_status('error', '没读到坐标 · 再截一次')
            self.overlay.set_values(self.overlay.rows[0][1], '—', '—', '—', '截图里没有找到网格坐标', bad=True)
            return None
        self.last_ocr = coord
        pt = self._coord_to_point(coord)
        if pt is None:
            if self.drone:
                self.drone['state'] = wait_state
            self.overlay.set_values(self.overlay.rows[0][1], coord, '—', '—', '坐标不在这张地图上', bad=True)
            self.overlay.set_status('error', '计算失败（坐标不在这张地图上）')
            return None
        if dr.get('state') == 'tgt':
            who = self.DRONE_WHO_TEXT[self.drone_who()]
            dr['tgt'] = pt
            dr['coord'] = coord
            dr['state'] = 'tgt_az'
            hint = '（I 易被误读成 L，请核对）' if coord.startswith('I') else ''
            self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
            self.lb_status.setText(f"快速定位：{coord}{hint} · {self._ocr_src(raw, ms)} —— 坐标已记，正在读{who}的方位角（2/2）…")
            self.overlay.set_status('idle', f"坐标已记 · 读{who}方位角（2/2）")
            self.overlay.set_values(self.overlay.rows[0][1], '—', '—', '—', f"坐标 {coord} 已记 · 正读第 2 步方位角")
            az_region = self.drone_az_region()
            if az_region:
                self.ocr_kind = 'az'
                self.ocr_flow = 'drone'
                self._start_worker(az_region, 'az', local=False, learn=False)
                return None
            self.drone = None
            self.pend = []
            self._commit_target(pt, 'single1')
            self.recompute()
            grid = self.map.grid_at(*self.target, self.grid_level())
            self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
            self.lb_status.setText(f"快速定位：{coord} · {self._ocr_src(raw, ms)} · 目标 {grid}（方位角框没了，没弹小地图）")
            return None
        self._drone_show_minimap(pt, coord, float((dr.get('az') or 0.0)), self._ocr_src(raw, ms))

    def bar_positions(self):
        lst = barrage.normalize_positions(self.cfg.get('bar_positions'))
        self.cfg['bar_positions'] = lst
        return lst

    def _weapon_by_key(self, key):
        for w in self.weapons:
            if not w.get('key') == key:
                continue
            return w
        # 找不到就返回 None。以前这里回退到第一门炮：弹幕里某个阵地写着一个
        # 不存在的武器 key 时，会拿别的炮的弹道去算，诸元错了也不报错。
        return None

    def _weapon_name(self, key):
        w = self._weapon_by_key(key)
        if w:
            return w.get('name')
        return key or '—'

    def bar_checked_uids(self):
        have = {p['uid'] for p in self.bar_positions()}
        return [u for u in (self.cfg.get('bar_checked') or []) if u in have]

    def bar_sub(self):
        if self.cfg.get('bar_sub') == 'scope':
            return 'scope'
        return 'drone'

    def set_bar_sub(self, sub):
        sub = 'scope' if sub == 'scope' else 'drone'
        self.cfg['bar_sub'] = sub
        save_config(self.cfg)
        self.btn_bar_drone.setChecked(sub == 'drone')
        self.btn_bar_scope.setChecked(sub == 'scope')
        if self.bar:
            self._close_minimap()
            self.bar = None
        self.lb_bar_hint.setText(self._bar_hint())
        self.lb_bar_step.setText(self._bar_step_text())
        self.bar_recompute()

    def _bar_item_text(self, p):
        return f"{p['uid']!s} · {(p.get('name') or '未命名')!s} · {self._weapon_name(p.get('weapon'))!s}"

    def refresh_bar_positions(self):
        if not hasattr(self, 'lw_bar_pos'):
            return None
        checked = set(self.bar_checked_uids())
        lst = self.bar_positions()
        self.lw_bar_pos.blockSignals(True)
        self.lw_bar_pos.clear()
        for p in lst:
            it = QListWidgetItem(self._bar_item_text(p))
            it.setToolTip(self._bar_item_text(p))
            it.setData(Qt.UserRole, p['uid'])
            it.setFlags((it.flags() | Qt.ItemIsUserCheckable))
            it.setCheckState(Qt.Checked if p['uid'] in checked else Qt.Unchecked)
            self.lw_bar_pos.addItem(it)
        self.lw_bar_pos.blockSignals(False)
        self.lb_bar_pos.setText(('%d / %d 个阵地 · 勾选 %d 个参与 · 宽度上限 %g m' % (len(lst), barrage.MAX_POSITIONS, len(checked), barrage.max_width(max(1, len(checked))))))

    def refresh_bar_gun_combo(self):
        if not hasattr(self, 'cb_bar_gun'):
            return None
        pick = (self.cfg.get('bar_gun') or '')
        self.cb_bar_gun.blockSignals(True)
        self.cb_bar_gun.clear()
        self.cb_bar_gun.addItem('— 手动点地图 —', '')
        for p in self.bar_positions():
            self.cb_bar_gun.addItem(self._bar_item_text(p), p['uid'])
        idx = self.cb_bar_gun.findData(pick)
        self.cb_bar_gun.setCurrentIndex(idx if idx >= 0 else 0)
        self.cb_bar_gun.blockSignals(False)

    def on_bar_gun_picked(self, idx):
        if (not hasattr(self, 'cb_bar_gun') or idx < 0):
            return None
        uid = (self.cb_bar_gun.itemData(idx) or '')
        self.cfg['bar_gun'] = uid
        save_config(self.cfg)
        if not uid:
            return None
        p = barrage.find(self.bar_positions(), uid)
        if not p:
            return None
        self.gun = (float(p['x']), float(p['z']))
        w = self._weapon_by_key(p.get('weapon'))
        if w:
            j = self.cb_weap.findData(w['key'])
            if j >= 0:
                self.cb_weap.setCurrentIndex(j)
        self.view.gun = self.gun
        self.view.update()
        self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
        self.lb_status.setText(f"阵地已摆到 {self._bar_item_text(p)!s}（{self.map.grid_at(*self.gun, 4) if self.map else '—'!s}）")
        self.bar_recompute()

    def on_bar_item_changed(self, item):
        if not hasattr(self, 'lw_bar_pos'):
            return None
        uids = []
        for i in range(self.lw_bar_pos.count()):
            it = self.lw_bar_pos.item(i)
            if it is None:
                continue
            if not it.checkState() == Qt.Checked:
                continue
            uids.append(it.data(Qt.UserRole))
        self.cfg['bar_checked'] = uids
        save_config(self.cfg)
        self.lb_bar_pos.setText(('%d / %d 个阵地 · 勾选 %d 个参与 · 宽度上限 %g m' % (len(self.bar_positions()), barrage.MAX_POSITIONS, len(uids), barrage.max_width(max(1, len(uids))))))
        self.lb_bar_hint.setText(self._bar_hint())
        self.bar_recompute()

    def _bar_sel_uid(self):
        it = self.lw_bar_pos.currentItem() if hasattr(self, 'lw_bar_pos') else None
        if it is not None:
            return it.data(Qt.UserRole)
        return ''

    def _bar_pending_point(self):
        """「手动点地图」时地图上刚点出来的那个点；下拉选了具体阵地时返回 None（走输入框）。"""
        if (self.cfg.get('bar_gun') or '').strip():
            return None
        if self.gun:
            return (float(self.gun[0]), float(self.gun[1]))

    def _bar_read_coord(self, title, default=''):
        txt, ok = QInputDialog.getText(self, '呼叫弹幕 · 阵地坐标', ('%s（3 级或 4 级都行，如 F7-2-6 或 F7-2-6-4）' % title), text=default)
        if not ok:
            return None
        txt = (txt or '').strip()
        if not txt:
            return None
        try:
            rx, rz = core.parse(txt)
        except Exception as e:
            QMessageBox.warning(self, '坐标不对', str(e))
            return None
        if self.map and not (0 <= rx <= self.map.spanx and 0 <= rz <= self.map.spanz):
            QMessageBox.warning(self, '不在图上', '这个坐标不在这张地图上')
            return None
        return (rx, rz)

    def _bar_pick_weapon(self, cur_key=''):
        names = [w['name'] for w in self.weapons]
        if not names:
            return None
        j = 0
        for i, w in enumerate(self.weapons):
            if not w.get('key') == cur_key:
                continue
            j = i
            break
        name, ok = QInputDialog.getItem(self, '呼叫弹幕 · 阵地武器', '这个阵地用哪门迫击炮？', names, j, False)
        if not ok:
            return None
        for w in self.weapons:
            if not w['name'] == name:
                continue
            return w

    def bar_add_position(self):
        lst = self.bar_positions()
        if len(lst) >= barrage.MAX_POSITIONS:
            self._bar_warn(('最多 %d 个阵地' % barrage.MAX_POSITIONS))
            return None
        pt = self._bar_pending_point()
        if pt is None:
            pt = self._bar_read_coord('新阵地的位置')
        if pt is None:
            return None
        if not self.map:
            return None
        name, ok = QInputDialog.getText(self, '呼叫弹幕 · 阵地名称', '给这个阵地起个名字：', text=('阵地%d' % (len(lst) + 1)))
        if not ok:
            return None
        w = self._bar_pick_weapon(self.weapon.get('key') if self.weapon else '')
        if w is None:
            return None
        uid = barrage.next_uid(lst)
        if not uid:
            return None
        lst.append({'uid': uid, 'name': ((name or '').strip() or ('阵地' + uid)), 'x': float(pt[0]), 'z': float(pt[1]), 'weapon': w['key']})
        self.cfg['bar_positions'] = lst
        self.cfg['bar_checked'] = (list((self.cfg.get('bar_checked') or [])) + [uid])
        save_config(self.cfg)
        self.refresh_bar_positions()
        self.refresh_bar_gun_combo()
        self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
        self.lb_status.setText(f"已新建阵地 {uid!s}（{self._weapon_name(w['key'])!s}）")
        self.bar_recompute()

    def bar_edit_position(self):
        uid = self._bar_sel_uid()
        if not uid:
            self._bar_warn('先在列表里点一个阵地，再按「修改」')
            return None
        lst = self.bar_positions()
        p = barrage.find(lst, uid)
        if not p:
            return None
        pt = self._bar_pending_point()
        if pt is None:
            dv = self.map.grid_at(p['x'], p['z'], 4) if self.map else ''
            pt = self._bar_read_coord(('改 %s 的位置' % uid), dv)
        if pt is None:
            return None
        w = self._bar_pick_weapon(p.get('weapon'))
        if w is None:
            return None
        name, ok = QInputDialog.getText(self, '呼叫弹幕 · 阵地名称', '阵地名字：', text=(p.get('name') or ''))
        if not ok:
            return None
        p['x'], p['z'] = float(pt[0]), float(pt[1])
        p['weapon'] = w['key']
        p['name'] = ((name or '').strip() or p.get('name') or ('阵地' + uid))
        self.cfg['bar_positions'] = lst
        save_config(self.cfg)
        self.refresh_bar_positions()
        self.refresh_bar_gun_combo()
        self.lb_status.setText(('已修改阵地 %s' % uid))
        self.bar_recompute()

    def bar_del_position(self):
        uid = self._bar_sel_uid()
        if not uid:
            self._bar_warn('先在列表里点一个阵地，再按「删除」')
            return None
        p = barrage.find(self.bar_positions(), uid)
        if not p:
            return None
        if QMessageBox.question(self, '删除阵地', ('确定删掉 %s？' % self._bar_item_text(p))) != QMessageBox.Yes:
            return None
        self.cfg['bar_positions'] = [x for x in self.bar_positions() if x['uid'] != uid]
        self.cfg['bar_checked'] = [u for u in (self.cfg.get('bar_checked') or []) if u != uid]
        if self.cfg.get('bar_gun') == uid:
            self.cfg['bar_gun'] = ''
        save_config(self.cfg)
        self.refresh_bar_positions()
        self.refresh_bar_gun_combo()
        self.lb_status.setText(('已删除阵地 %s' % uid))
        self.bar_recompute()

    def _bar_warn(self, msg):
        self.lb_status.setStyleSheet('color:#ff8f8f;font-size:12px;')
        self.lb_status.setText(msg)
        self.lb_bar_step.setStyleSheet('color:#ff8f8f;font-size:12px;')
        self.lb_bar_step.setText(msg)
        print(('[bar] ' + msg), flush=True)

    def _bar_hint(self):
        n = max(1, len(self.bar_checked_uids()))
        sub = self.bar_sub()
        reg = self.cfg.get('region') if sub == 'drone' else self.cfg.get('s_region')
        sub_txt = '无人机视角（读屏幕坐标）' if sub == 'drone' else '望远镜视角（方位角 + 坐标 + 距离）'
        return '参与 %d 门 · 宽度 %g–%g m · 起始点来源：%s · 框：%s' % (n, barrage.MIN_WIDTH, barrage.max_width(n), sub_txt, (self._rect_text(reg) if reg else '未设'))

    def _bar_step_text(self):
        hk = self.hk_texts.get(41, 'Ctrl+F1')
        cur = self.bar or {}
        st = cur.get('state')
        if st is None:
            return '按 %s 开始：① 标起始点 → ② 小地图上点一下定宽度 → ③ 同一张图上再点一侧定方向 → ④ 输推进速度' % hk
        if st == 'az':
            return '正在读方位角（%s）…' % hk
        if st == 'pos':
            return '正在读坐标 —— 再按一次 %s 重读' % hk
        if st == 'tgt_az':
            return '正在读方位角（决定小地图朝向）—— 再按 %s 重读，⌫ 退回读坐标' % hk
        if st == 'dist':
            return ('输你到起始点的距离（3 位，如 155）—— 已输 %s' % (cur.get('digits') or '—'))
        if st == 'start':
            return '在小地图上点一下起始点的真实位置（修正识别误差）'
        if st == 'width':
            return ('在小地图上点一下：这一点与它的对称点就是起始线（宽 %g–%g m）' % (barrage.MIN_WIDTH, barrage.max_width(max(1, len(self.bar_checked_uids())))))
        if st == 'dir':
            return '再在这张图上点一侧：点的那边就是炸的方向，点的深度 = 推进距离'
        if st == 'speed':
            return ('输推进速度（米/轮，≥%g）—— 已输 %s；按 %s 确认' % (barrage.MIN_SPEED, (cur.get('spd_txt') or '—'), hk))
        if st == 'done':
            plan = (cur.get('plan') or [])
            return ('第 %d/%d 轮已复制到剪贴板 —— %s 下一轮，⌫ 结束' % ((cur.get('round') or 1), len(plan), hk))
        return ''

    def _bar_refresh(self):
        if not hasattr(self, 'lb_bar_step'):
            return None
        self.lb_bar_step.setStyleSheet('color:#ffd166;font-size:12px;')
        self.lb_bar_step.setText(self._bar_step_text())
        self.lb_bar_hint.setText(self._bar_hint())
        self.bar_recompute()

    def bar_recompute(self):
        if getattr(self, 'mode', 'arty') != 'barrage':
            return None
        if not hasattr(self, 'lb_bar_step'):
            return None
        cur = self.bar or {}
        st = cur.get('state')
        start = cur.get('start')
        g1 = ((self.map.grid_at(start[0], start[1], 4) if self.map else '—') if start else '—')
        g2 = '—'
        if cur.get('width'):
            g2 = '起始线 %g m' % cur['width']
            if cur.get('depth'):
                g2 += ' × 深 %g m' % cur['depth']
        g3 = '—'
        plan = (cur.get('plan') or [])
        if plan:
            g3 = ('%g m/轮 · %d 门 · %d 轮' % ((cur.get('speed') or 0.0), len((plan[0].get('rows') or [])), len(plan)))
        g4 = '—'
        if plan and cur.get('round'):
            g4 = ('第 %d/%d 轮' % (cur['round'], len(plan)))
        rows = [('起始点', g1), ('弹幕区', g2), ('推进', g3), ('本轮', g4)]
        sol = None
        if st == 'done' and plan and cur.get('round'):
            k = max(1, min(int(cur['round']), len(plan)))
            pl = plan[(k - 1)]
            items = []
            for r in (pl.get('rows') or []):
                w = r['w']
                res = (r.get('res') or {})
                if (r.get('err') or not r.get('sol')):
                    items.append((r['uid'], ('打不到（%s）' % (r.get('err') or '超出射界')), False))
                else:
                    items.append((r['uid'], ('%s %s · 方位 %.1f° · %.0f m' % (core.angle_label(w), core.angle_display(w, r['sol']), (res.get('brg') or 0.0), (res.get('dist') or 0.0))), True))
            sol = (('诸元 · 离起始线 %g m' % (pl.get('off') or 0.0)), items)
        notes = None
        if st in ('az', 'pos', 'dist'):
            notes = '识别起始点中…'
        elif st == 'tgt_az':
            notes = '读取方位角（决定小地图朝向）…'
        elif st == 'start':
            notes = '小地图上点一下起始点真实位置'
        elif st == 'width':
            notes = '小地图上点一下定宽度'
        elif st == 'dir':
            notes = '同一张图上再点一侧定方向'
        elif st == 'speed':
            notes = '输推进速度（米/轮，≥%g）' % barrage.MIN_SPEED
        elif st == 'done':
            notes = '已复制到剪贴板 · 上一轮 / 下一轮换轮次'
        self.overlay.set_rows(rows, (notes or '按流程键开始：标起始点 → 宽度 → 方向 → 速度'), sol=sol)
        if st == 'done':
            self.overlay.set_status('done', ('第 %d 轮已就绪' % (cur.get('round') or 1)))
            return None
        if st:
            self.overlay.set_status('busy', self._bar_step_text()[:40])
            return None
        self.overlay.set_status('idle', '呼叫弹幕待命')

    def bar_step(self):
        if getattr(self, 'mode', 'arty') != 'barrage':
            return None
        cur = self.bar or {}
        st = cur.get('state')
        if st is None:
            self.bar_start()
            return None
        if st == 'az':
            self._bar_capture_az()
            return None
        if st == 'pos':
            self._bar_capture_pos()
            return None
        if st == 'tgt_az':
            self._bar_capture_drone_az()
            return None
        if st == 'speed':
            self._bar_confirm_speed()
            return None
        if st == 'done':
            self.bar_next()
            return None
        self._bar_refresh()

    def bar_start(self):
        if getattr(self, 'mode', 'arty') != 'barrage':
            return None
        if self.worker and self.worker.isRunning():
            self._bar_warn('上一个识别还没结束，等一下再按')
            return None
        if not self.bar_checked_uids():
            self._bar_warn('先在「阵地库」里勾选要参与这次弹幕的阵地（至少 1 个）')
            return None
        if not self.map:
            self._bar_warn('先在上面选一张地图')
            return None
        sub = self.bar_sub()
        self.bar = {'state': ('az' if sub == 'scope' else 'pos'), 'az': None, 'pos': None, 'dist': None, 'start': None, 'a': None, 'b': None, 'width': 0.0, 'normal': None, 'depth': 0.0, 'speed': 0.0, 'plan': [], 'round': 0, 'spd_txt': '', 'digits': ''}
        self._set_digit_keys(())
        self._close_minimap()
        if sub == 'scope':
            if not self.az_region():
                self.bar = None
                self._bar_warn('望远镜视角要先用「方位角区域」（面板里框一下）')
                return None
            self._bar_capture_az()
        else:
            if not self.cfg.get('region'):
                self.bar = None
                self._bar_warn('无人机视角要先用「目标坐标区域」（面板里框一下）')
                return None
            self._bar_capture_pos()
        self._bar_refresh()

    def _bar_capture_az(self):
        reg = self.az_region()
        if not reg:
            self._bar_warn('没有「方位角区域」—— 先在面板里框一下望远镜的方位角')
            return None
        self._ocr_job(reg, 'az', '呼叫弹幕：识别方位角（1/3）', '先读你望远镜里的方位角', 'bar', local=True)

    def _bar_capture_pos(self):
        reg = self.az_region() if self.bar_sub() == 'scope' else self.cfg.get('region')
        if self.bar_sub() == 'scope':
            reg = self.scope_grid_region()
        if not reg:
            self._bar_warn('没有坐标区域 —— 先在面板里框一下')
            return None
        note = '再读你在望远镜里的坐标（2/3）' if self.bar_sub() == 'scope' else '把准星对准起始点'
        self._ocr_job(reg, 'pos', '呼叫弹幕：识别坐标', note, 'bar', local=True)

    def on_bar_ocr_ok(self, coord, raw, ms):
        cur = self.bar or {}
        st = cur.get('state')
        if st == 'az':
            self.on_bar_az_ok(coord, raw, ms)
            return None
        if st == 'tgt_az':
            self.on_bar_az_ok(coord, raw, ms)
            return None
        if st == 'pos':
            self.on_bar_pos_ok(coord, raw, ms)
            return None

    def on_bar_az_ok(self, coord, raw, ms):
        hk = self.hk_texts.get(41, 'Ctrl+F1')
        cur = self.bar or {}
        drone_az = cur.get('state') == 'tgt_az'
        if not coord:
            if drone_az:
                self._bar_drone_az_failed(('没读到（模型说：%s）' % (raw or '')[:30]))
                return None
            self.bar = None
            self._bar_warn(f"没读到方位角（模型说：{(raw or '')[:40]!s}）—— 再按 {hk!s} 重来")
            return None
        try:
            az = (float(str(coord).strip().rstrip('°')) % 360.0)
        except Exception:
            if drone_az:
                self._bar_drone_az_failed(('读出来不是数字（%s）' % coord))
                return None
            self.bar = None
            self._bar_warn(('方位角读出来不是数字（%s）' % coord))
            return None
        else:
            cur['az'] = az
            if drone_az:
                self._bar_begin_start(cur.get('pos'))
                return None
            cur['state'] = 'pos'
            self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
            self.lb_status.setText(('方位角 %.0f° · %s —— 再按 %s 截你的坐标（2/3）' % (az, self._ocr_src(raw, ms), hk)))
            self._bar_refresh()

    def on_bar_pos_ok(self, coord, raw, ms):
        hk = self.hk_texts.get(41, 'Ctrl+F1')
        if not coord:
            self.bar = None
            self._bar_warn(f"没读到坐标（模型说：{(raw or '')[:40]!s}）—— 再按 {hk!s} 重来")
            return None
        pt = self._coord_to_point(coord)
        if pt is None:
            return None
        cur = self.bar or {}
        cur['pos'] = pt
        if self.bar_sub() == 'drone':
            cur['az'] = None
            src_txt = f"起始点 {coord!s} · {self._ocr_src(raw, ms)!s}"
            if self.drone_az_region():
                cur['state'] = 'tgt_az'
                who = self.DRONE_WHO_TEXT.get(self.drone_who(), '无人机')
                self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
                self.lb_status.setText((src_txt + (' —— 正在读%s的方位角（决定小地图朝向）…' % who)))
                self._bar_refresh()
                self._bar_capture_drone_az()
                return None
            self._bar_begin_start(pt, note='（没框方位角区域，小地图正北朝上）')
            return None
        cur['state'] = 'dist'
        cur['digits'] = ''
        self._set_digit_keys('0123456789')
        self.lb_status.setText(('你的坐标 %s —— 输你到起始点的距离（3 位，如 155）' % coord))
        self._bar_refresh()

    def _bar_capture_drone_az(self):
        """无人机视角第二步：再截一次方位角，只用来把小地图转成「正上方=方位角」。

    用的是无人机那块方位角框（面板「间瞄火力支援·无人机视角」里框的 cmd/scout 两块之一），
    读法同间瞄：大字+粗黑描边，本地模板库对不上 → 只走 AI，也不往望远镜的库学。"""
        reg = self.drone_az_region()
        if not reg:
            self._bar_drone_az_failed('方位角区域没设')
            return None
        if not self.ed_key.text().strip():
            self._bar_drone_az_failed('没填 DeepSeek 密钥')
            return None
        who = self.DRONE_WHO_TEXT.get(self.drone_who(), '无人机')
        self._ocr_job(reg, 'az', ('呼叫弹幕：识别%s的方位角' % who), '读取无人机画面上的方位角（决定小地图朝向）', 'bar', local=False, learn=False)

    def _bar_drone_az_failed(self, why):
        """方位角没读到：坐标已经读到就照常弹图，只是正北朝上（宽度/方向照样能点）。"""
        cur = self.bar or {}
        cur['az'] = None
        pos = cur.get('pos')
        if not pos:
            self.bar = None
            self._bar_warn(f"没读到方位角（{why!s}）—— 再按 {self.hk_texts.get(41, 'Ctrl+F1')!s} 重来")
            return None
        self._bar_begin_start(pos, note=('（方位角没读到：%s —— 小地图正北朝上）' % why))

    def _bar_up_text(self):
        """小地图正上方现在是什么：读到的方位角，或者正北（没读到 / 没框方位角框）。"""
        cur = self.bar or {}
        az = cur.get('az')
        if az is None:
            return '正北'
        return ('方位 %.0f°' % (float(az) % 360.0))

    def _bar_stage_kw(self):
        """begin_stage 的朝向参数：望远镜=本流程读到的方位角（看完还能按 ↑ 转正北）；
    无人机=自动读的那块方位角。已经在分步点选里就不再重置（保住玩家按 ↑ 的结果）。"""
        cur = self.bar or {}
        cont = bool(self.minimap.isVisible() and self.minimap.stage)
        return {'az': (None if cont else cur.get('az')), 'north_key': (self.bar_sub() == 'scope')}

    def bar_digit(self, ch):
        if getattr(self, 'mode', 'arty') != 'barrage':
            return None
        if not ch.isdigit():
            return None
        cur = self.bar or {}
        st = cur.get('state')
        hk = self.hk_texts.get(41, 'Ctrl+F1')
        if st == 'dist':
            d = ((cur.get('digits') or '') + ch)
            d = d[:3]
            cur['digits'] = d
            if len(d) == 3:
                self._bar_from_dist()
                return None
            self._bar_refresh()
            return None
        if st == 'speed':
            t = ((cur.get('spd_txt') or '') + ch)
            t = (t.lstrip('0') or '0')
            cur['spd_txt'] = t[:3]
            self._bar_refresh()
            return None
        self._bar_warn(('现在不用输数字 —— 按 %s 是开始/确认' % hk))

    def _bar_from_dist(self):
        cur = self.bar or {}
        pos = cur.get('pos')
        az = cur.get('az')
        try:
            dist = float((cur.get('digits') or 0))
        except Exception:
            dist = 0.0
        if not pos or az is None or dist <= 0:
            self.bar = None
            self._bar_warn('距离或方位角不对，重来一次')
            return None
        rad = math.radians(az)
        start = ((pos[0] + (dist * math.sin(rad))), (pos[1] - (dist * math.cos(rad))))
        if self.map:
            start = (max(0.0, min(start[0], self.map.spanx)), max(0.0, min(start[1], self.map.spanz)))
        self._set_digit_keys(())
        self._bar_begin_start(start)

    def _bar_begin_start(self, start, note=''):
        """起始点读到之后的第一眼：先在小地图上点一下它的真实位置，把识别误差修掉。

    不点也行 —— 直接接着点宽度就按识别值走（大方向通常是对的，差个几米）。
    note 是追加在状态栏末尾的说明（方位角没读到 / 没框方位角区域时用）。"""
        cur = self.bar or {}
        cur['start'] = (float(start[0]), float(start[1]))
        cur['state'] = 'start'
        n = max(1, len(self.bar_checked_uids()))
        span = barrage.span_for(n)
        self.view.target = cur['start']
        self.view.grid_level = 4
        self.view.target_title = '起始点'
        self.view.update()
        grid = self.map.grid_at(cur['start'][0], cur['start'][1], 4) if self.map else ''
        ok = self.minimap.begin_stage('start', cur['start'], self.map, self.view.img, span, title='弹幕 · 起始点 %s（估）' % grid, prompt='点一下起始点的真实位置（修正识别误差）', **self._bar_stage_kw())
        if not ok:
            self.bar = None
            self._bar_warn('底图没加载好，小地图弹不出来 —— 换个地图或等一下再试')
            return None
        self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
        self.lb_status.setText(f"起始点 {grid!s}（识别值）· 小地图上方={self._bar_up_text()!s} —— 点一下真实位置修正{note!s}")
        self._bar_refresh()

    def _bar_begin_width(self, start, off=0.0):
        cur = self.bar or {}
        cur['start'] = (float(start[0]), float(start[1]))
        cur['state'] = 'width'
        n = max(1, len(self.bar_checked_uids()))
        span = barrage.span_for(n)
        self.view.target = cur['start']
        self.view.grid_level = 4
        self.view.target_title = '起始点'
        self.view.update()
        grid = self.map.grid_at(cur['start'][0], cur['start'][1], 4) if self.map else ''
        ok = self.minimap.begin_stage('width', cur['start'], self.map, self.view.img, span, title='弹幕 · 起始点 %s' % grid, prompt='点一下定宽度（关于起始点对称，上限 %g m）' % barrage.max_width(n), **self._bar_stage_kw())
        if not ok:
            self.bar = None
            self._bar_warn('底图没加载好，小地图弹不出来 —— 换个地图或等一下再试')
            return None
        self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
        self.lb_status.setText('起始点 %s%s · 上方=%s —— 小地图上点一下定宽度（%g–%g m）' % (grid, ('（已修正 %g m）' % off) if off >= 0.5 else '', self._bar_up_text(), barrage.MIN_WIDTH, barrage.max_width(n)))
        self._bar_refresh()

    def on_bar_stage_pick(self, kind, rx, rz):
        cur = self.bar or {}
        if not cur:
            return None
        st = cur.get('state')
        n = max(1, len(self.bar_checked_uids()))
        start = cur.get('start')
        if not start:
            return None
        if kind == 'start' and st == 'start':
            rx = max(0.0, min(rx, self.map.spanx)) if self.map else rx
            rz = max(0.0, min(rz, self.map.spanz)) if self.map else rz
            off = math.hypot((rx - start[0]), (rz - start[1]))
            self._bar_begin_width((rx, rz), off=off)
            return None
        if kind == 'width' and st == 'width':
            a, b, w, msg = barrage.segment(start, (rx, rz), n)
            if msg:
                self.minimap.set_stage('width', msg=msg)
                return None
            cur['a'], cur['b'], cur['width'] = a, b, w
            cur['state'] = 'dir'
            self.minimap.set_stage('dir', a=a, b=b, prompt='再点一侧：往那边炸，点的深度 = 推进距离', msg='')
            self.lb_status.setText(('宽度 %g m —— 同一张图上再点一侧定方向' % w))
            self._bar_refresh()
            return None
        if kind == 'dir' and st == 'dir':
            nrm, depth, msg = barrage.direction(start, cur['a'], cur['b'], (rx, rz))
            if msg:
                self.minimap.set_stage('dir', msg=msg)
                return None
            cur['normal'], cur['depth'] = nrm, depth
            cur['state'] = 'speed'
            cur['spd_txt'] = ''
            self._set_digit_keys('0123456789')
            self.minimap.set_stage('dir', normal=nrm, depth=depth, prompt=('输推进速度（米/轮，≥%g），按流程键确认' % barrage.MIN_SPEED), msg='')
            self.lb_status.setText(('推进距离 %g m —— 输推进速度（米/轮，≥%g）' % (depth, barrage.MIN_SPEED)))
            self._bar_refresh()
            return None
        return None

    def _bar_confirm_speed(self):
        cur = self.bar or {}
        txt = (cur.get('spd_txt') or '').strip()
        if not txt:
            self._bar_warn(('先输推进速度（米/轮，最小 %g）' % barrage.MIN_SPEED))
            return None
        try:
            spd = float(int(txt))
        except Exception:
            self._bar_warn('推进速度要是数字')
            return None
        else:
            if spd < barrage.MIN_SPEED:
                self._bar_warn(('推进速度最小 %g 米/轮' % barrage.MIN_SPEED))
                return None
            cur['speed'] = spd
            self._set_digit_keys(())
            self._bar_build_plan()
            cur['state'] = 'done'
            cur['round'] = 1
            self.minimap.hide()
            self._bar_show_round(1)
            self._bar_refresh()

    def _bar_build_plan(self):
        cur = self.bar or {}
        poslist = self.bar_positions()
        guns = [barrage.find(poslist, u) for u in self.bar_checked_uids()]
        guns = [g for g in guns if g]
        if (not guns or not cur.get('normal')):
            cur['plan'] = []
            return None
        plans = barrage.aims(cur['start'], cur['a'], cur['b'], cur['normal'], cur['depth'], cur['speed'], len(guns))
        out = []
        for pl in plans:
            rows = []
            for g, aim in zip(guns, pl['aims']):
                w = self._weapon_by_key(g.get('weapon'))
                if not w:
                    continue
                gun = (float(g['x']), float(g['z']))
                try:
                    hg = self.map.h_at(gun[0], gun[1]) if self.map else 0.0
                    if self.map:        # 阵地库里的炮位也可能在桥面/集装箱上
                        try:
                            hg += los.support_at(self.map, self.los_store.get(self.map),
                                                 gun[0], gun[1])[0]
                        except Exception:
                            pass
                    ht = self.map.h_at(aim[0], aim[1]) if self.map else 0.0
                    res = core.evaluate(w, gun, aim, hg, ht)
                    rows.append({'uid': g['uid'], 'w': w, 'aim': aim, 'res': res, 'sol': res.get('sol'), 'err': res.get('err')})
                except Exception as e:
                    rows.append({'uid': g['uid'], 'w': w, 'aim': aim, 'res': None, 'sol': None, 'err': str(e)})
                continue
            out.append({'round': pl['round'], 'off': pl['off'], 'rows': rows})
        cur['plan'] = out

    def _bar_show_round(self, k):
        cur = self.bar or {}
        plan = (cur.get('plan') or [])
        if not plan:
            self.lb_bar_text.setText('没有可用的阵地 / 武器，算不出诸元')
            return None
        k = max(1, min(int(k), len(plan)))
        cur['round'] = k
        pl = plan[(k - 1)]
        lines = [('第 %d/%d 轮 · 目标线离起始线 %g m' % (k, len(plan), (pl.get('off') or 0.0)))]
        for r in (pl.get('rows') or []):
            w = r['w']
            res = (r.get('res') or {})
            if (r.get('err') or not r.get('sol')):
                lines.append(f"{r['uid']!s} · {w.get('name')!s} · 打不到（{(r.get('err') or '超出射界')!s}）")
            else:
                ang = core.angle_display(w, r['sol'])
                lines.append(('%s · %s · %s %s · 方位 %.1f° · %.0f m' % (r['uid'], w.get('name'), core.angle_label(w), ang, (res.get('brg') or 0.0), (res.get('dist') or 0.0))))
        txt = """
""".join(lines)
        self.lb_bar_text.setText(txt)
        try:
            set_clipboard_text(txt)
        except Exception:
            pass
        self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
        self.lb_status.setText(('第 %d/%d 轮诸元已复制到剪贴板' % (k, len(plan))))
        try:
            self.scroll.ensureWidgetVisible(self.lb_bar_text, 0, 10)
        except Exception:
            return None
        else:
            return None

    def bar_next(self):
        cur = self.bar or {}
        plan = (cur.get('plan') or [])
        if not plan:
            self._bar_warn('还没有弹幕计划 —— 先按流程键走一遍')
            return None
        self._bar_show_round(min(len(plan), ((cur.get('round') or 1) + 1)))
        self._bar_refresh()

    def bar_prev(self):
        cur = self.bar or {}
        plan = (cur.get('plan') or [])
        if not plan:
            self._bar_warn('还没有弹幕计划 —— 先按流程键走一遍')
            return None
        self._bar_show_round(max(1, ((cur.get('round') or 1) - 1)))
        self._bar_refresh()

    def bar_copy(self):
        txt = self.lb_bar_text.text() if hasattr(self, 'lb_bar_text') else ''
        if not txt:
            self._bar_warn('还没有诸元可复制')
            return None
        try:
            set_clipboard_text(txt)
            self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
            self.lb_status.setText('本轮诸元已复制到剪贴板')
        except Exception as e:
            self._bar_warn(('复制失败：%s' % e))
        return None

    def bar_clear(self, msg='已清除呼叫弹幕'):
        self._close_minimap()
        self.bar = None
        self._set_digit_keys(())
        if hasattr(self, 'lb_bar_text'):
            self.lb_bar_text.setText('')
        if getattr(self, 'mode', 'arty') == 'barrage':
            self.view.target = None
            self.view.update()
            self._bar_refresh()
        self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
        self.lb_status.setText(msg)

    def bar_backspace(self):
        cur = self.bar or {}
        st = cur.get('state')
        if st in ('dist', 'speed'):
            key = 'digits' if st == 'dist' else 'spd_txt'
            cur[key] = (cur.get(key) or '')[:-1]
            self._bar_refresh()
            return None
        if st == 'tgt_az':
            hk = self.hk_texts.get(41, 'Ctrl+F1')
            cur['state'] = 'pos'
            cur['az'] = None
            self._bar_refresh()
            self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
            self.lb_status.setText(('已退回读坐标 —— 再按 %s 重新截起始点' % hk))
            return None
        if st in ('start', 'width', 'dir'):
            self.bar = None
            self._close_minimap()
            self._set_digit_keys(())
            self._bar_warn('已取消这次弹幕 —— 重新按流程键标起始点')
            return None
        if st == 'done':
            self.bar = None
            self._bar_refresh()
            self.lb_status.setText('弹幕流程已结束')
            return None
        self._bar_warn('现在没有可退的一步')

    def _start_worker(self, region, kind, local=True, learn=True):
        if not region:
            return False
        key = self.ed_key.text().strip()
        self.worker = OcrWorker(tuple(region), key, kind=kind, bank=self.digit_bank, pos_bank=self.pos_bank, local=local, learn=learn, parent=self)
        self.worker.ok.connect(self.on_ocr_ok)
        self.worker.fail.connect(self.on_ocr_fail)
        self.worker.start()
        return True

    def start_ocr(self, mode='quick'):
        if getattr(self, 'mode', 'arty') == 'recon':
            self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
            self.lb_status.setText('现在是「侦察报告」模式：不算密位/方位，请用侦察流程键')
            return None
        if getattr(self, 'arty_sub', 'drone') == 'scope':
            self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
            self.lb_status.setText(f"现在是「望远镜视角」子模式：用 {self.hk_texts.get(21, 'F8')} 走望远镜流程（面板「间瞄子模式」可切回无人机视角）")
            return None
        if self.worker and self.worker.isRunning():
            return None
        key = self.ed_key.text().strip()
        if not key:
            self.lb_status.setText('先填 DeepSeek 密钥')
            return None
        if self.drone and self.drone.get('state') == 'done':
            self.drone = None
            self._close_minimap()
        if self.drone and self.drone.get('state') in ('tgt', 'tgt_az') and not self.drone_az_region():
            self.drone = None
        self.ocr_what = ''
        self.ocr_mode = 'exact' if mode == 'exact' else 'quick'
        az_region = self.drone_az_region() if self.ocr_mode == 'quick' else None
        if az_region and self._drone_target_first():
            who = self.DRONE_WHO_TEXT[self.drone_who()]
            if (self.drone or {}).get('state') == 'tgt_az':
                coord = (self.drone.get('coord') or '')
                self.ocr_kind = 'az'
                self.ocr_flow = 'drone'
                self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
                self.lb_status.setText(f"正在截取{who}的方位角（快速定位 2/2，坐标 {coord} 已记）…")
                self.overlay.set_status('busy', '识别方位角（2/2）')
                self.overlay.set_values(self.overlay.rows[0][1], '识别中…', '—', '—', f"坐标 {coord} 已记 · 读完方位角就弹 {self.DRONE_SPAN:g} m 小地图")
                if not self.overlay.isVisible():
                    self.overlay.show()
                    self.overlay.raise_()
                self._start_worker(az_region, 'az', local=False, learn=False)
                return None
            self.ocr_kind = 'pos'
            self.ocr_flow = 'drone'
            self.drone = {'az': None, 'state': 'tgt', 'tgt': None, 'coord': ''}
            self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
            self.lb_status.setText('正在截取目标坐标（快速定位 1/2）…（先本地识别，不行才走 AI）')
            self.overlay.set_status('busy', '识别目标坐标（1/2）')
            self.overlay.set_values(self.overlay.rows[0][1], '识别中…', '—', '—', f"先读目标坐标 · 读完再读{who}的方位角（它一直在转）")
            if not self.overlay.isVisible():
                self.overlay.show()
                self.overlay.raise_()
            self._start_worker(self.cfg['region'], 'pos')
            return None
        if az_region and not self._drone_has_az():
            self.ocr_kind = 'az'
            self.ocr_flow = 'drone'
            self.drone = {'az': None, 'state': 'az'}
            who = self.DRONE_WHO_TEXT[self.drone_who()]
            self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
            self.lb_status.setText(f"正在截取{who}的方位角（快速定位 1/2）…")
            self.overlay.set_status('busy', '识别方位角（1/2）')
            self.overlay.set_values(self.overlay.rows[0][1], '识别中…', '—', '—', f"先读{who}画面的方位角 · 下一步读目标坐标")
            if not self.overlay.isVisible():
                self.overlay.show()
                self.overlay.raise_()
            self._start_worker(az_region, 'az', local=False, learn=False)
            return None
        self.ocr_kind = 'pos'
        self.ocr_flow = 'tel' if not (az_region and self._drone_has_az()) else 'drone'
        self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
        if self.ocr_mode == 'quick':
            if self.ocr_flow == 'drone':
                az = float(self.drone['az'])
                self.lb_status.setText(f"正在截取目标坐标（快速定位 2/2，方位 {az:.0f}° 已记）…（先本地识别，不行才走 AI）")
                self.overlay.set_status('busy', '识别目标坐标（2/2）')
                self.overlay.set_values(self.overlay.rows[0][1], '识别中…', '—', '—', f"方位 {az:.0f}° 已记 · 读完坐标就弹 60 m 小地图")
            else:
                self.lb_status.setText('正在截取坐标（快速定位，1 次）…（先本地识别，不行才走 AI）')
                self.overlay.set_status('busy', '识别中 · 快速定位（1 次）')
                self.overlay.set_values(self.overlay.rows[0][1], '识别中…', '—', '—', '截 1 次，直接把读到的坐标当目标')
        else:
            n = min((len(self.pend) + 1), PEND_TOTAL)
            self.lb_status.setText(f"正在截取第 {n} 个坐标（{n}/{PEND_TOTAL}，准确定位）…（先本地识别，不行才走 AI）")
            self.overlay.set_status('busy', f"截第 {n} 个（{n}/{PEND_TOTAL}）· 正在识别")
            tips = {1: f"请把准星对准目标再按热键（共 {PEND_TOTAL} 次）", 2: f"第 2 次截取：准星再挪一点（2/{PEND_TOTAL}）", 3: f"第 3 次截取：最后一次，马上出结果（3/{PEND_TOTAL}）"}
            got = [self.map.grid_at(q[0], q[1]) for q in self.pend]
            marks = '①②③'[:len(got)]
            prev = f"{marks[-1]} {got[-1]}（{len(got)}/{PEND_TOTAL}）" if got else '等待第 1 个…'
            self.overlay.set_values(self.overlay.rows[0][1], prev, '—', '—', tips.get(n, ''))
        if not self.overlay.isVisible():
            self.overlay.show()
            self.overlay.raise_()
        self._start_worker(self.cfg['region'], 'pos')

    def do_capture(self, *_):
        self.start_ocr('quick')

    def _scope_hint(self):
        hk = self.hk_texts.get(21, 'F8')
        if getattr(self, 'arty_sub', 'drone') != 'scope':
            return f"切到「望远镜视角」子模式后可用：{hk} 开始 / 下一步"
        miss = []
        if not self.cfg.get('s_region_az'):
            miss.append('方位角区域')
        if not self.cfg.get('s_region'):
            miss.append('玩家坐标区域')
        head = (('还缺：' + '、'.join(miss)) + '（在本组里框一下）') if miss else '两块区域都就绪'
        return f"{head}。{hk} 开始：先读方位角 → 再按一次读你的坐标 → 小键盘 1–9 补第 4 段 → 小键盘输 3 位距离"

    def scope_grid_region(self):
        r = self.cfg.get('s_region')
        if r:
            return list(r)

    def az_region(self):
        r = self.cfg.get('s_region_az')
        if r:
            return list(r)

    def tel_reset(self, keep_target=True, msg=''):
        """结束望远镜流程；keep_target=True 时保留上次算好的目标。"""
        self._close_minimap()
        had = self.tel is not None
        self.tel = None
        self.tel_warn = ''
        self._set_digit_keys(())
        self.view.player = None
        self.view.target_title = '目标'
        if not keep_target:
            self.target = None
            self.tgt_mode = None
            self.view.target = None
        self.view.update()
        self.recompute()
        if msg and (had or not keep_target):
            self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
            self.lb_status.setText(msg)
            return None
        return None

    def tel_step(self):
        """快速定位键在望远镜模式下走这里。"""
        st = (self.tel or {}).get('state')
        self._tel_log(f"按热键（流程状态 {(st or '空闲')}）")
        if st in (None, 'done'):
            self._close_minimap()
            if not self.gun:
                self.lb_status.setStyleSheet('color:#ff8f8f;font-size:12px;')
                self.lb_status.setText('望远镜流程要先在地图上点出阵地')
                return None
            if not self.az_region():
                self.lb_status.setStyleSheet('color:#ff8f8f;font-size:12px;')
                self.lb_status.setText('先在「截图区域 · 望远镜视角」里框选 ② 方位角，再按热键')
                self.overlay.set_status('error', '没设方位角区域 · 望远镜那组框一下')
                self.overlay.show()
                return None
            if not self.scope_grid_region():
                self.lb_status.setStyleSheet('color:#ff8f8f;font-size:12px;')
                self.lb_status.setText('先在「截图区域 · 望远镜视角」里框选 ① 玩家坐标')
                self.overlay.set_status('error', '没设玩家坐标区域 · 望远镜那组框一下')
                self.overlay.show()
                return None
            self.tel = {'state': 'az', 'az': None, 'pos': None, 'pos_txt': '', 'digits': '', 'dist': None}
            self.tel_warn = ''
            self.tel_capture_az()
            return None
        if st == 'az':
            self.tel_capture_az()
            return None
        if st == 'pos':
            self.tel_capture_pos()
            return None
        if st == 'digit':
            self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
            self.lb_status.setText('现在等你按小键盘 1–9（主键盘数字也行）补坐标最后一位')
            return None
        self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
        self.lb_status.setText(f"现在等你输入 3 位距离（小键盘或主键盘，已输 {len(self.tel.get('digits', ''))}/3）")

    def _tel_worker(self, region, kind, busy, note):
        return self._ocr_job(region, kind, busy, note, 'tel')

    def _tel_log(self, msg):
        """望远镜流程每走一步都写一行日志：出问题时 run.log 能看出卡在哪一步。"""
        try:
            print(f"[tel] {msg}", flush=True)
        except Exception:
            return None

    def _ocr_job(self, region, kind, busy, note, flow='tel', local=True, learn=True):
        if not region:
            self.lb_status.setStyleSheet('color:#ff8f8f;font-size:12px;')
            self.lb_status.setText('还没框选这块截图区域（面板里对应的「框选」按钮）')
            return False
        if self.worker and self.worker.isRunning():
            return False
        key = self.ed_key.text().strip()
        if not key and kind not in ('az', 'pos'):
            self.lb_status.setText('先填 DeepSeek 密钥')
            return False
        self.ocr_kind = kind
        self.ocr_flow = flow
        try:
            print(f"[ocr] 开始 flow={flow} kind={kind} area={tuple(region)}", flush=True)
        except Exception:
            pass
        self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
        self.lb_status.setText((busy + ('…（走 AI 识别，约 1~2 秒）' if not local else '…（先本地识别，不行才走 AI）')))
        self.overlay.set_status('busy', busy)
        if flow in ('recon', 'bar'):
            self.overlay.set_note(note)
        else:
            self.overlay.set_values(self.overlay.rows[0][1], '识别中…', '—', '—', note)
        if not self.overlay.isVisible():
            self.overlay.show()
            self.overlay.raise_()
        self._start_worker(region, kind, local=local, learn=learn)
        return True

    def tel_capture_az(self):
        region = self.az_region()
        self._tel_worker(region, 'az', '识别方位角（1/3）', '把你视角里的方位角读数框进「方位角区域」')

    def tel_capture_pos(self):
        self._tel_worker(self.scope_grid_region(), 'pos', '识别你的坐标（2/3）', f"方位 {self.tel.get('az', 0):.0f}° 已记 · 现在读你自己的坐标")

    @staticmethod
    def _ocr_src(raw, ms):
        """把识别来源显示成一句话：本地模板匹配 vs 大模型。"""
        if raw == 'local':
            return '本地识别'
        return f"{ms} ms"

    def on_az_ok(self, coord, raw, ms):
        if not coord:
            self.lb_status.setStyleSheet('color:#ff8f8f;font-size:12px;')
            self.lb_status.setText(f"没读到方位角（模型说：{raw[:40]}）—— 再按一次 {self.hk_texts.get(21, 'F8')} 重试")
            self.overlay.set_status('error', '没读到方位角 · 再截一次')
            return None
        try:
            az = (float(coord) % 360.0)
        except ValueError:
            self.overlay.set_status('error', '方位角不合法 · 再截一次')
            return None
        else:
            self.tel['az'] = az
            self.tel['state'] = 'pos'
            self._tel_log(f"方位角 {coord}° · {self._ocr_src(raw, ms)}")
            self.recompute()
            src = '本地识别' if raw == 'local' else f"AI 识别 {ms} ms"
            self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
            self.lb_status.setText(f"方位角 {az:.0f}° · {src} —— 再按 {self.hk_texts.get(21, 'F8')} 截你自己的位置坐标（2/3）")

    def tel_pos_ok(self, coord, raw, ms):
        if not coord:
            self.lb_status.setStyleSheet('color:#ff8f8f;font-size:12px;')
            self.lb_status.setText(f"没读到坐标（模型说：{raw[:40]}）—— 再按 {self.hk_texts.get(21, 'F8')} 重试（方位角已记）")
            self.overlay.set_status('error', '没读到坐标 · 再截一次')
            return None
        try:
            rx, rz = core.parse(coord)
        except core.ParseError as e:
            self.lb_status.setStyleSheet('color:#ff8f8f;font-size:12px;')
            self.lb_status.setText(str(e))
            self.overlay.set_status('error', '坐标格式不对 · 再截一次')
            return None
        else:
            if rx > (self.map.spanx + 1) or rz > (self.map.spanz + 1):
                self.lb_status.setStyleSheet('color:#ff8f8f;font-size:12px;')
                self.lb_status.setText(f"{coord} 不在这张地图上")
                self.overlay.set_status('error', '坐标不在这张地图上')
                return None
            point = (min(rx, self.map.spanx), min(rz, self.map.spanz))
            self.tel['pos'] = point
            self.tel['pos_txt'] = coord
            self._tel_log(f"位置读成 {coord}（{len(coord.split('-'))} 段，{self._ocr_src(raw, ms)}）")
            if len(coord.split('-')) >= 4:
                self.tel['state'] = 'dist'
                self.tel['digits'] = ''
                self.tel['manual4'] = False
                self._set_digit_keys('0123456789')
                self.recompute()
                self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
                self.lb_status.setText(f"位置 {coord} · {self._ocr_src(raw, ms)} —— 用小键盘（或主键盘）输你到目标的距离（3 位，如 155 / 090 / 001）")
                return None
            self.tel['state'] = 'digit'
            self._set_digit_keys('123456789')
            self.recompute()
            self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
            self.lb_status.setText(f"位置 {coord} · {self._ocr_src(raw, ms)} —— 按小键盘 1–9（主键盘数字也行）补全坐标最后一位（{coord}-?，即 3.7 m 格）")

    def backspace(self):
        """⌫ 删掉刚输入的那个数字/字母，方便重打（几条流程通用）。"""
        if getattr(self, 'mode', 'arty') == 'barrage':
            self.bar_backspace()
            return None
        if getattr(self, 'mode', 'arty') == 'recon':
            self._recon_backspace()
            return None
        self._tel_backspace()

    def _backspace_hint(self):
        self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
        self.lb_status.setText('现在没有刚输入的数字/字母可删（退格只在输入的那几步有用）')

    @staticmethod
    def _strip4(txt):
        parts = (txt or '').split('-')
        if len(parts) >= 4:
            return '-'.join(parts[:3])
        return txt or ''

    def _tel_backspace(self):
        mm = getattr(self, 'minimap', None)
        dr = self.drone or {}
        if self.drone and dr.get('state') in ('tgt', 'tgt_az'):
            self.drone = None
            self.recompute()
            self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
            self.lb_status.setText(f"已取消这次快速定位 → 再按 {self.hk_texts.get(1, 'F1')} 从「读目标坐标」重新开始（目标/阵地都没动）")
            return None
        if self.drone and dr.get('state') == 'done':
            first = self._drone_target_first()
            if first:
                self.drone = None
            else:
                self.drone['state'] = 'pos'
            if mm is not None and mm.isVisible():
                mm.hide()
            self.recompute()
            hk = self.hk_texts.get(1, 'F1')
            tgt_txt = self.map.grid_at(*self.target) if self.target else '—'
            self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
            if first:
                self.lb_status.setText(f"已取消小地图 → 再按 {hk} 重新截目标坐标（接着会再读一次方位角）；目标暂用估测值 {tgt_txt}")
                return None
            az = dr.get('az')
            self.lb_status.setText(f"已取消小地图 → 再按 {hk} 重新截目标坐标（方位 {float(az):.0f}° 保留）；目标暂用估测值 {tgt_txt}")
            return None
        st = (self.tel or {}).get('state')
        if mm is not None and mm.isVisible() and st == 'done':
            self.tel['state'] = 'dist'
            self.tel['digits'] = ''
            self.tel['dist'] = None
            self._set_digit_keys('0123456789')
            mm.hide()
            self.recompute()
            self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
            self.lb_status.setText(f"已取消小地图 → 重新输你到目标的距离（3 位，如 155）；目标暂时还用原来那个 {self.map.grid_at(*self.target)}")
            return None
        if st == 'dist':
            dg = (self.tel.get('digits') or '')
            if dg:
                self.tel['digits'] = dg[:-1]
                n = len(self.tel['digits'])
                self.recompute()
                self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
                self.lb_status.setText(f"已删一位 → 距离 {self.tel['digits']}，再输 {(3 - n)} 位（共 3 位）" if self.tel['digits'] else '已删一位 → 距离已清空，重新输 3 位')
                return None
            if self.tel.get('manual4'):
                old = (self.tel.get('pos_txt') or '')
                new = self._strip4(old)
                try:
                    rx, rz = core.parse(new)
                except core.ParseError:
                    self._backspace_hint()
                    return None
                self.tel['pos_txt'] = new
                self.tel['pos'] = (min(rx, self.map.spanx), min(rz, self.map.spanz))
                self.tel['manual4'] = False
                self.tel['state'] = 'digit'
                self._set_digit_keys('123456789')
                self.recompute()
                self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
                self.lb_status.setText(f"已删掉刚补的第 4 段 → {new}-?，用小键盘 1–9 重新补")
                return None
        self._backspace_hint()

    def _recon_mini_drop(self):
        """流程跳到别的状态去了（出报告 / 清除 / 选更新目标 / 选型号…）：
    小地图静默收掉，免得它一直浮在桌面上。要先把 self.recon 换成新状态再调它。"""
        mm = getattr(self, 'minimap', None)
        if mm is not None and mm.isVisible():
            (self.recon or {}).pop('mini', None)
            mm.hide()
            return None
        return None

    def _recon_minimap_cancel(self):
        """小地图被取消（按退格 / 右键 / 切走 / 切模式）：收掉标记、退回能重来的那一步。
    「更新已有目标」那条路取消时记录一个字都不动（弹图只是修正，不是必须点）。"""
        cur = self.recon or {}
        st = cur.get('state')
        if st not in ('type', 'item', 'common', 'upd_mini'):
            return False
        upd = st == 'upd_mini'
        cur.pop('mini', None)
        hk = self.hk_texts.get(11, 'F5')
        az = float((cur.get('az') or 0.0))
        self.overlay.clear_options()
        if getattr(self, 'recon_sub', 'scope') == 'drone':
            cur['state'] = 'target'
            cur.pop('pend4', None)
            self._set_digit_keys(())
            self.recon_recompute()
            self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
            self.lb_status.setText((f"已取消小地图 → 再按 {hk} 重新截敌目标坐标（方位角 {az:.0f}° 保留）；" + ('这条记录还没改' if upd else f"目标暂用估测值 {(cur.get('grid') or '—')}")))
            return True
        cur['state'] = 'dist'
        cur['digits'] = ''
        cur['dist'] = None
        self._set_digit_keys('0123456789')
        self.recon_recompute()
        self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
        self.lb_status.setText((f"已取消小地图 → 重新输你到目标的距离（3 位，如 155；方位角 {az:.0f}° 保留）；" + ('这条记录还没改' if upd else f"目标暂用估测值 {(cur.get('grid') or '—')}")))
        return True

    def _recon_tgt_mini_cancel(self):
        """3 级坐标弹出的小地图被取消（退格 / 右键 / 又按标记键 / 切走）：
    收掉标记，目标退回「等截坐标」，等玩家再按标记键重截一次。
    方位角保留（同一次观察里不用重读）。"""
        cur = self.recon or {}
        if cur.get('state') != 'tgt_mini':
            return False
        cur.pop('mini', None)
        cur['state'] = 'target'
        cur['target'] = None
        cur['grid'] = ''
        cur['pos_txt'] = ''
        self.view.target = None
        self.view.update()
        self._set_digit_keys(())
        self._set_char_keys(False)
        self.recon_recompute()
        hk = self.hk_texts.get(11, 'F5')
        az = cur.get('az')
        self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
        self.lb_status.setText((((f"已取消小地图 → 再按 {hk} 重新截敌目标坐标" + (f"（方位角 {float(az):.0f}° 保留）" if az is not None else ''))) + ('；这条记录还没改' if cur.get('upd_tid') else '')))
        mm = getattr(self, 'minimap', None)
        if mm is not None and mm.isVisible():
            mm.hide()
        return True

    def _recon_backspace(self):
        st = (self.recon or {}).get('state')
        mm = getattr(self, 'minimap', None)
        cur = self.recon or {}
        if cur.get('mini') and st in ('type', 'item', 'common', 'upd_mini') and mm is not None and mm.isVisible():
            self._recon_minimap_cancel()
            mm.hide()
            return None
        if st == 'tgt_mini':
            self._recon_tgt_mini_cancel()
            return None
        if st == 'raz' and cur.get('wait_az'):
            self.recon.update({'wait_az': None, 'state': 'target', 'target': None, 'grid': '', 'pos_txt': ''})
            self.view.target = None
            self.view.update()
            self._set_digit_keys(())
            self._set_char_keys(False)
            self.recon_recompute()
            self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
            self.lb_status.setText(f"已取消 → 再按 {self.hk_texts.get(11, 'F5')} 重新截敌目标坐标（先读坐标、再读方位角）")
            return None
        if st in ('digit4', 'digit4_ok'):
            if self.recon.get('pend4'):
                self.recon['pend4'] = ''
                self.recon['state'] = 'digit4'
                self._set_digit_keys('123456789')
                self.recon_recompute()
                self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
                self.lb_status.setText(f"已清掉刚补的第 4 位 → {self.recon.get('pos_txt')}-?，重新敲 1–9")
                return None
            self._backspace_hint()
            return None
        if st == 'mcoord':
            buf = (self.recon.get('buf') or '')
            if buf:
                self.recon['buf'] = buf[:-1]
                shown = self._recon_buf_text(self.recon['buf'])
                self.recon_recompute()
                self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
                self.lb_status.setText(f"已删一位 → 格号 {(shown or '（空）')} · 再输 {(6 - len(self.recon['buf']))} 位（共 6 位）")
                return None
            self._backspace_hint()
            return None
        if st == 'dist':
            dg = (self.recon.get('digits') or '')
            if dg:
                self.recon['digits'] = dg[:-1]
                n = len(self.recon['digits'])
                self.recon_recompute()
                self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
                self.lb_status.setText(f"已删一位 → 距离 {self.recon['digits']}，再输 {(3 - n)} 位（共 3 位）" if self.recon['digits'] else '已删一位 → 距离已清空，重新输 3 位')
                return None
            if self.recon.get('manual4'):
                old = (self.recon.get('pos_txt') or '')
                new = self._strip4(old)
                try:
                    rx, rz = core.parse(new)
                except core.ParseError:
                    self._backspace_hint()
                    return None
                self.recon['pos_txt'] = new
                self.recon['pos'] = (min(rx, self.map.spanx), min(rz, self.map.spanz))
                self.recon['manual4'] = False
                self.recon['state'] = 'digit'
                self.view.player = self.recon['pos']
                self._set_digit_keys('123456789')
                self.recon_recompute()
                self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
                self.lb_status.setText(f"已删掉刚补的第 4 段 → {new}-?，用小键盘 1–9 重新补")
                return None
        if st in ('buddy', 'rpt_buddy'):
            buf = (self.recon.get('buf') or '')
            if buf:
                self.recon['buf'] = buf[:-1]
                shown = self._recon_buf_text(self.recon['buf'])
                self.recon_recompute()
                self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
                self.lb_status.setText(f"已删一位 → 队友坐标 {(shown or '（空）')} · 再输 {(6 - len(self.recon['buf']))} 位（共 6 位）")
                return None
        self._backspace_hint()

    def tel_digit(self, d):
        st = (self.tel or {}).get('state')
        if st == 'digit':
            if d == '0':
                return None
            txt = f"{self.tel['pos_txt']}-{d}"
            try:
                rx, rz = core.parse(txt)
            except core.ParseError as e:
                self.lb_status.setStyleSheet('color:#ff8f8f;font-size:12px;')
                self.lb_status.setText(str(e))
                return None
            self.tel['pos'] = (min(rx, self.map.spanx), min(rz, self.map.spanz))
            self.tel['pos_txt'] = txt
            self._tel_log(f"补第 4 位 {d} → 位置 {txt}")
            self.tel['state'] = 'dist'
            self.tel['digits'] = ''
            self.tel['manual4'] = True
            self._set_digit_keys('0123456789')
            self.recompute()
            self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
            self.lb_status.setText(f"位置 {txt} —— 用小键盘输你到目标的距离（3 位：百→个，如 155 / 090 / 001）；按错可按退格删一位")
            return None
        if st == 'dist':
            self.tel['digits'] = (self.tel.get('digits') or '') + d
            n = len(self.tel['digits'])
            self._tel_log(f"距离第 {n} 位 = {d}（{self.tel['digits']}）")
            if n < 3:
                self.recompute()
                self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
                self.lb_status.setText(f"距离 {self.tel['digits']}… 再输 {(3 - n)} 位（共 3 位）")
                return None
            self.tel_finish()
            return None
        return None

    def tel_finish(self):
        digits = (self.tel.get('digits') or '')
        if len(digits) != 3:
            return None
        dist = float(int(digits))
        az = float((self.tel.get('az') or 0.0))
        pos = self.tel.get('pos')
        if pos is None:
            self.tel_reset(True, '望远镜流程缺位置，已重置')
            return None
        rad = math.radians(az)
        tx = (pos[0] + (dist * math.sin(rad)))
        tz = (pos[1] - (dist * math.cos(rad)))
        self.tel['dist'] = dist
        self.tel['state'] = 'done'
        self._set_digit_keys(())
        self.view.player = pos
        self.view.target_title = '敌目标'
        self._commit_target((tx, tz), 'tel')
        self.recompute()
        grid = self.map.grid_at(*self.target)
        self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
        self.lb_status.setText(f"望远镜：位置 {self.tel['pos_txt']} · 方位 {az:.0f}° · 距离 {dist:.0f} m → 敌目标 {grid}")
        if dist <= self.MINIMAP_OVER:
            self._tel_log(f"完成：距离 {dist:.0f} m ≤ {self.MINIMAP_OVER:.0f} m，按规则不弹小地图 · 目标 {grid}")
            self.lb_status.setText((self.lb_status.text() + f"（距离 ≤{self.MINIMAP_OVER:.0f} m，没弹小地图）"))
            return None
        if self._open_minimap(self.target, az, pos, north_key=True):
            self._tel_log(f"完成：距离 {dist:.0f} m · 目标 {grid} · 小地图已弹出")
            self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
            self.lb_status.setText(f"敌目标 {grid}（估测）—— 小地图已弹出：点一下目标的真实位置（右键取消）；找不到就按 ⌫ 重新输距离")
            self.overlay.set_status('done', f"点小地图修正目标 · {grid}")
            return None
        self._tel_log(f"完成：距离 {dist:.0f} m · 目标 {grid} · 小地图没弹出来（底图未加载或没有目标）")

    MINIMAP_OVER = 100.0
    def _close_minimap(self):
        mm = getattr(self, 'minimap', None)
        if mm is not None and mm.isVisible():
            mm.hide()
            return None
        return None

    def _open_minimap(self, tgt, az, me=None, span=None, north_key=False):
        """以 tgt 为心弹一块小地图（图的正上方 = 方位 az，即玩家正前方）；
    窗口中心一次性挪到鼠标位置。span 不填就是望远镜的 120 m 见方，
    无人机快速定位传 DRONE_SPAN（60 m）。north_key=True（望远镜那两张图）时，
    等玩家点的这段时间里按一下方向键「上」就把图转成正北朝上。"""
        mm = getattr(self, 'minimap', None)
        if mm is None or not self.map or self.view.img is None or not tgt:
            return False
        losd = self._los_desc()
        center = tgt
        # 只有调用方没指定视野时才为了「同时看见目标和挡点」放大窗口：
        # 无人机那两张 60 m 的图要用来精确点目标，放大 12 倍就没法点了——只叠遮挡层。
        if losd and span is None:
            center, span2 = self._los_view(tgt, losd)
            if span2:
                span = span2
        ok = mm.show_at(center, self.map, self.view.img, est_grid=self.map.grid_at(*tgt, self.grid_level()), az=az, me=me, span=span, north_key=north_key, los=losd)
        if ok:
            mm.set_spread(self._spread_desc())
        return ok

    def _los_update(self, res, hgun):
        """解算变了就重跑一次弹道遮挡（地形 + 建筑/桥梁），结果留在 self.los_res。

        只判「这组诸元真正要打的那条弹道」；打不到 / 侦察 / 弹幕时清空，
        免得小地图和状态行还挂着上一次的判定。整段有异常也不许影响出诸元。"""
        self.los_res = None
        if (getattr(self, 'mode', 'arty') != 'arty' or not res or not self.gun or (not self.target)):
            return None
        sol = res.get('sol') or res.get('alt')
        if not sol:
            return None
        try:
            lm = self.los_store.get(self.map)
        except Exception as e:
            print(f"[los] 遮挡数据打不开（{self.map.slug}）：{e}", flush=True)
            return None
        if lm is None:
            return None
        w = self.weapon
        t0 = time.perf_counter()
        try:
            return self._los_run(lm, w, hgun, sol, res, t0)
        except Exception as e:                   # 遮挡判定坏掉也不许影响出诸元
            print(f"[los] 判定异常（{getattr(self.map, 'slug', '?')}）：{e}", flush=True)
            self.los_res = None
            return None

    def _los_run(self, lm, w, hgun, sol, res, t0):
        """_los_update 的实体（单独拆出来只是为了套一层 try）。"""
        main = los.trace(self.map, lm, w, self.gun, self.target, hgun, sol)
        alt = None
        if main.get('blocked'):
            # 被挡时顺手看看「另一条角档」能不能越过：能就值得提醒玩家切过去
            other = res.get('alt')
            if other is None:
                try:
                    _arc = 'high' if res.get('solName') == '低角' else 'low'
                    _ht = self.htg if getattr(self, 'htg', None) is not None else self.map.h_at(*self.target)
                    other = core.evaluate(w, self.gun, self.target, hgun, _ht, _arc).get('sol')
                except Exception:
                    other = None
            if other is not None and other.get('key') != sol.get('key'):
                alt = los.trace(self.map, lm, w, self.gun, self.target, hgun, other)
        self.los_res = {'main': main, 'alt': alt, 'dist': float(res.get('dist') or 0.0), 'ms': (time.perf_counter() - t0) * 1000.0}
        return None

    def _los_note(self):
        """状态行 / 浮窗追加的「弹道被挡」一句话；没挡就是空串。"""
        d = getattr(self, 'los_res', None) or {}
        m = d.get('main') or {}
        if not m.get('blocked'):
            return ''
        txt = f"⚠弹道被挡 {float(m.get('s') or 0):.0f} m · {m.get('label') or '障碍'}"
        a = d.get('alt') or {}
        if a and (not a.get('blocked')):
            txt += '（另一条角档可越过）'
        return txt

    def _los_desc(self):
        """交给小地图画的「被挡」描述；通畅就 None。"""
        d = getattr(self, 'los_res', None) or {}
        m = d.get('main') or {}
        if not m.get('blocked') or not self.target or (not self.gun):
            return None
        a = d.get('alt') or {}
        return {'hit': tuple(m.get('hit') or (0.0, 0.0)), 's': float(m.get('s') or 0.0), 'label': m.get('label') or '障碍', 'kind': m.get('kind') or '', 'foot': m.get('foot'), 'tgt': (float(self.target[0]), float(self.target[1])), 'gun': (float(self.gun[0]), float(self.gun[1])), 'shadow': m.get('shadow'), 'alt_ok': bool(a and (not a.get('blocked')))}

    def _los_view(self, tgt, losd):
        """被挡时小地图要同时装下目标（修正点）和挡点，否则玩家看不出「挡在哪」。

        返回 (center, span)：center 挪到两点之间偏目标一侧，span 把两者都框住；
        装不下就退化为 None（沿用调用方原来的视野）。"""
        tx, tz = (float(tgt[0]), float(tgt[1]))
        try:
            hx, hz = (float(losd['hit'][0]), float(losd['hit'][1]))
        except (KeyError, TypeError, ValueError, IndexError):
            return (tgt, None)
        d = math.hypot((hx - tx), (hz - tz))
        if not math.isfinite(d) or d < 5.0:
            return (tgt, None)
        span = min(760.0, (d * 1.15 + 90.0))
        k = 0.45
        return ((tx + (hx - tx) * k, tz + (hz - tz) * k), span)

    def _spread_desc(self):
        """当前解的散布椭圆（交给小地图画）：None = 这组数字画不了。

        只在「间瞄火力支援」画（侦察 / 呼叫弹幕有自己的图，返回 None）。
        横轴 cross 用 sprA（角散布在弹着点横向拉开的半轴），纵轴 along 用
        sprB（高角/低角那两条边界的距离差），跟面板「散布 A×B m」同一个口径；
        ang 是纵轴指向（炮位→目标的世界方位角，东 0、南 +90）。
        dist 对不上当前阵地/目标就拒绝——那说明 sol 还是上一次的，画出来会骗人。"""
        if getattr(self, 'mode', 'arty') != 'arty':
            return None
        res = getattr(self, 'sol', None) or {}
        sol = res.get('sol')
        if not sol or not self.gun or not self.target:
            return None
        gx, gz = (float(self.gun[0]), float(self.gun[1]))
        tx, tz = (float(self.target[0]), float(self.target[1]))
        d = math.hypot((tx - gx), (tz - gz))
        if d < 1.0 or abs((float(res.get('dist') or 0.0) - d)) > 0.5:
            return None
        return {'c': (tx, tz), 'cross': float(sol.get('sprA') or 0.0), 'along': float(sol.get('sprB') or 0.0), 'ang': math.degrees(math.atan2((tz - gz), (tx - gx)))}

    def _refresh_minimap_spread(self):
        """解算变了：小地图开着就同步重画散布（关着就什么也不做）。"""
        mm = getattr(self, 'minimap', None)
        if mm is None or not mm.isVisible():
            return None
        mm.set_spread(self._spread_desc())
        mm.set_los(self._los_desc())

    def on_minimap_picked(self, rx, rz):
        """玩家在小地图上点出了目标的真实位置：只改目标，阵地一点不动。"""
        cur = self.recon or {}
        if self.mode == 'recon' and cur.get('mini') and cur.get('state') in ('type', 'item', 'common', 'upd_mini') and cur.get('target'):
            old = cur['target']
            new = (min(max(rx, 0.0), self.map.spanx), min(max(rz, 0.0), self.map.spanz))
            cur['target'] = new
            cur['grid'] = self.map.grid_at(*new, 4)
            cur.pop('mini', None)
            pos = cur.get('pos')
            if pos:
                dx, dz = new[0] - pos[0], new[1] - pos[1]
                cur['dist'] = math.hypot(dx, dz)
                cur['az'] = ((math.degrees(math.atan2(dx, (-dz))) + 360.0) % 360.0)
            self.view.target = new
            self.view.update()
            off = math.hypot((new[0] - old[0]), (new[1] - old[1]))
            if cur.get('state') == 'upd_mini':
                self._finish_update(cur['grid'])
                if (self.recon or {}).get('state') == 'saved':
                    txt = self.lb_status.text()
                    self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
                    self.lb_status.setText(f"{txt} · 小地图修正 {off:.0f} m")
                return None
            self.recon_recompute()
            self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
            self.lb_status.setText(f"已按小地图修正目标 {cur['grid']}（离估测点 {off:.0f} m） —— 在浮窗里按数字选「这是什么敌方目标」（0 = 只标记位置）")
            return None
        if self.mode == 'recon' and cur.get('state') == 'tgt_mini' and cur.get('mini') and cur.get('target'):
            old = cur['target']
            new = (min(max(rx, 0.0), self.map.spanx), min(max(rz, 0.0), self.map.spanz))
            cur['target'] = new
            cur['grid'] = self.map.grid_at(*new, 4)
            cur.pop('mini', None)
            cur['mini_used'] = True
            self.view.target = new
            self.view.update()
            off = math.hypot((new[0] - old[0]), (new[1] - old[1]))
            if cur.get('upd_tid'):
                self._finish_update(cur['grid'])
                if (self.recon or {}).get('state') == 'saved':
                    txt = self.lb_status.text()
                    self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
                    self.lb_status.setText(f"{txt} · 小地图修正 {off:.0f} m")
                return None
            self._after_target(cur['grid'], 'drone')
            self.lb_status.setText(((self.lb_status.text() or '') + f" · 小地图修正 {off:.0f} m"))
            return None
        if self.drone and self.drone.get('state') == 'done':
            old = self.target
            self._commit_target((rx, rz), 'single1')
            self.recompute()
            off = math.hypot((rx - old[0]), (rz - old[1])) if old else 0.0
            grid = self.map.grid_at(*self.target, self.grid_level())
            sol = (self.sol or {}).get('sol')
            extra = ''
            if sol:
                extra = f" · {core.angle_label(self.weapon)} {core.angle_display(self.weapon, sol)}"
            self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
            self.lb_status.setText(f"已按小地图修正目标 {grid}（离估测点 {off:.0f} m）{extra} · 阵地没动")
            return None
        if not self.tel or not self.tel.get('state') == 'done':
            return None
        old = self.target
        self._commit_target((rx, rz), 'tel')
        self.recompute()
        off = math.hypot((rx - old[0]), (rz - old[1])) if old else 0.0
        grid = self.map.grid_at(*self.target, self.grid_level())
        sol = (self.sol or {}).get('sol')
        extra = ''
        if sol:
            w = self.weapon
            extra = f" · {core.angle_label(w)} {core.angle_display(w, sol)}"
        self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
        self.lb_status.setText(f"已按小地图修正目标 {grid}（离估测点 {off:.0f} m）{extra} · 阵地没动")

    def on_minimap_closed(self):
        """小地图收起来后把浮窗状态改回普通显示（别一直挂着「点小地图修正目标」）。"""
        if getattr(self, '_shutting', False):
            return None
        if self.mode == 'barrage':
            cur = self.bar or {}
            if cur.get('state') in ('start', 'width', 'dir'):
                self.bar = None
                self._set_digit_keys(())
                self._bar_warn('小地图关了：这次弹幕取消，要重来再按一次流程键')
                self.bar_recompute()
            return None
        if self.mode == 'recon':
            cur = self.recon or {}
            if cur.get('state') == 'tgt_mini':
                self._recon_tgt_mini_cancel()
                return None
            if cur.get('state') == 'upd_mini':
                self._recon_minimap_cancel()
                return None
            if cur.get('state') in ('type', 'item', 'common'):
                cur.pop('mini', None)
                self.recon_recompute()
            return None
        if self.drone and self.drone.get('state') == 'done':
            self.drone = None
            self.recompute()
            return None
        if self.tel and self.tel.get('state') == 'done':
            self.recompute()
            return None
        return None

    def tel_prompt(self, gun_txt=None):
        """望远镜流程进行中：面板与浮窗都显示下一步该干什么。"""
        st = (self.tel or {}).get('state')
        if st in (None, 'done'):
            return None
        hk = self.hk_texts.get(21, 'F8')
        if gun_txt is None:
            _hg = self.hgun if getattr(self, 'hgun', None) is not None else self.map.h_at(*self.gun)
            gun_txt = f"{self.map.grid_at(*self.gun)} · {_hg:.0f} m" if self.gun else '—'
        az = float((self.tel.get('az') or 0.0))
        pos_txt = (self.tel.get('pos_txt') or '')
        dg = (self.tel.get('digits') or '')
        if st == 'az':
            note, status = '读罗盘上的方位角'
            tgt = f"① 按 {hk} 截取你视角的方位角（1/3）"
            short = '① 按热键读方位角'
        elif st == 'pos':
            tgt = f"方位 {az:.0f}°"
            status = f"② 方位 {az:.0f}° 已记 · 再按 {hk} 截你自己的位置坐标（2/3）"
            note = f"方位 {az:.0f}° 已记 · 再按 {hk} 读你的坐标"
            short = f"② 方位 {az:.0f}° · 读你的坐标"
        elif st == 'digit':
            tgt = f"{pos_txt}-?"
            status = f"③ 按小键盘 1–9（主键盘也行）补全坐标最后一位：{pos_txt}-?"
            note = '小键盘/主键盘 1–9 补第 4 位（3.7 m 格）· 输错在距离那步退格可退回'
            short = f"③ 小键盘 1–9 补位：{pos_txt}-?"
        else:
            tgt = f"{pos_txt} · {(dg or '距离')}"
            status = f"④ 用小键盘输你到目标的距离（3 位，百→个，如 155 / 090 / 001）：已输 {len(dg)}/3"
            note = f"小键盘输距离 第 {min((len(dg) + 1), 3)} 位 / 3 · 退格删一位"
            short = f"④ 小键盘输距离 {(dg or '-')}（{len(dg)}/3）"
        self.vals['tgt'].setText(tgt)
        self.vals['brg'].setText('—')
        self.vals['mil'].setText('—')
        self.lb_extra.setText('')
        self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
        self.lb_status.setText(status)
        self.overlay.set_values(gun_txt, tgt, '—', '—', note)
        self.overlay.set_status('idle', short)

    def tel_danger(self, res):
        """玩家自己是否落在弹着点散布椭圆内（不影响密位/方位）。"""
        if (not self.tel or not self.tel.get('state') == 'done' or not self.tel.get('pos')):
            return ''
        sol = res.get('sol')
        if not sol or not self.gun or not self.target:
            return ''
        pa = max(0.5, float((sol.get('sprA') or 0.0)))
        pb = max(0.5, float((sol.get('sprB') or 0.0)))
        gx, gz = self.gun
        tx, tz = self.target
        d = math.hypot((tx - gx), (tz - gz))
        if d < 1:
            return ''
        ux, uz = (tx - gx) / d, (tz - gz) / d
        px, pz = self.tel['pos']
        vx, vz = px - tx, pz - tz
        along = ((vx * ux) + (vz * uz))
        perp = (((-vx) * uz) + (vz * ux))
        if (((perp / pa) ** 2) + ((along / pb) ** 2)) <= 1.0:
            return f"⚠ 你在散布圈内（离弹着点 {math.hypot(vx, vz):.0f} m）"
        return ''

    def clear_pending(self):
        if getattr(self, 'mode', 'arty') == 'recon':
            self.recon_clear()
            return None
        if self.tel:
            self.tel_reset(True, '已取消望远镜流程（上次算好的目标保留）')
            return None
        n = len(self.pend)
        self.pend = []
        if not self.target and self.last_target:
            _unpack, _unpack0 = self.last_target
            self.target = _unpack
            self.tgt_mode = _unpack0
            del _unpack, _unpack0
            self.view.target = self.target
            self.view.update()
        self.recompute()
        self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
        if n:
            keep = f"，目标 {self.map.grid_at(*self.target)} 保留" if self.target else ''
            self.lb_status.setText(f"已清除 {n} 个未完成的坐标{keep}")
            return None
        self.lb_status.setText('没有未完成的坐标要清除（目标是上次算好的，保留）')

    def _coord_to_point(self, coord):
        try:
            rx, rz = core.parse(coord)
        except core.ParseError as e:
            self.lb_status.setStyleSheet('color:#ff8f8f;font-size:12px;')
            self.lb_status.setText(str(e))
            return None
        else:
            if rx > (self.map.spanx + 1) or rz > (self.map.spanz + 1):
                self.lb_status.setStyleSheet('color:#ff8f8f;font-size:12px;')
                self.lb_status.setText(f"{coord} 不在这张地图上")
                return None
            return (min(rx, self.map.spanx), min(rz, self.map.spanz))

    def on_ocr_ok(self, coord, raw, ms):
        if getattr(self, '_shutting', False):
            # 关窗口时识别结果才回来：别再往流程里塞一步（会在退出途中又起一个线程）。
            return None
        if getattr(self, 'ocr_flow', 'tel') == 'bar':
            self.on_bar_ocr_ok(coord, raw, ms)
            return None
        if getattr(self, 'ocr_flow', 'tel') == 'recon' and self.recon:
            self.on_recon_ok(coord, raw, ms)
            return None
        if getattr(self, 'ocr_flow', 'tel') == 'drone':
            if getattr(self, 'ocr_kind', 'grid') == 'az':
                self.on_drone_az_ok(coord, raw, ms)
                return None
            self.on_drone_pos_ok(coord, raw, ms)
            return None
        if getattr(self, 'ocr_kind', 'grid') == 'az':
            self.on_az_ok(coord, raw, ms)
            return None
        if self.tel and self.tel.get('state') == 'pos':
            self.tel_pos_ok(coord, raw, ms)
            return None
        quick = self.ocr_mode == 'quick'
        n = 1 if quick else min((len(self.pend) + 1), PEND_TOTAL)
        if not coord:
            self.lb_status.setStyleSheet('color:#ff8f8f;font-size:12px;')
            if quick:
                self.lb_status.setText(f"没读到坐标（模型说：{raw[:40]}）—— 再按一次 {self.hk_text} 重试")
                self.overlay.set_status('error', '没读到坐标 · 再截一次')
            else:
                extra = f"（已记的 {len(self.pend)} 个还留着，再按一次重试第 {n} 个）" if self.pend else ''
                self.lb_status.setText(f"没读到坐标（模型说：{raw[:40]}）{extra}")
                self.overlay.set_status('error', f"第 {n} 个没读到 · 再截一次")
            self.overlay.set_values(self.overlay.rows[0][1], '无坐标', '—', '—', '截图里没有找到网格坐标', bad=True)
            return None
        self.last_ocr = coord
        pt = self._coord_to_point(coord)
        if pt is None:
            self.overlay.set_values(self.overlay.rows[0][1], coord, '—', '—', '坐标不在这张地图上', bad=True)
            self.overlay.set_status('error', '计算失败（坐标不在这张地图上）')
            return None
        if quick:
            self.pend = []
            self._commit_target(pt, 'single1')
            self.recompute()
            hint = '（I 易被误读成 L，请核对）' if coord.startswith('I') else ''
            self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
            self.lb_status.setText(f"快速定位：{coord} · {self._ocr_src(raw, ms)}{hint}")
            return None
        self.pend.append((pt[0], pt[1], coord))
        if len(self.pend) < PEND_TOTAL:
            self.target = None
            self.tgt_mode = None
            self.view.target = None
            self.view.update()
            self.recompute()
            hint = '（I 易被误读成 L，请核对）' if coord.startswith('I') else ''
            k = len(self.pend)
            self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
            self.lb_status.setText(f"第 {k} 个坐标 {coord} · {self._ocr_src(raw, ms)}{hint} —— 还差 {(PEND_TOTAL - k)} 个，再按 {self.hk_texts[2]} 截 {(PEND_TOTAL - k)} 次（共 {PEND_TOTAL} 次，取三点外心）")
            return None
        pts = list(self.pend)
        self.pend = []
        target, mode, msg = combine_targets(pts)
        self._commit_target(target, mode)
        self.recompute()
        self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
        self.lb_status.setText(f"{msg} → {self.map.grid_at(*self.target, 3)} · {self._ocr_src(raw, ms)}")

    def on_ocr_fail(self, msg):
        if getattr(self, '_shutting', False):
            return None
        try:
            print(f"[ocr] 识别失败：{msg}", flush=True)
        except Exception:
            pass
        if getattr(self, 'mode', 'arty') == 'barrage' and (self.bar or {}).get('state') == 'tgt_az':
            self._bar_drone_az_failed(msg)
            return None
        self.lb_status.setStyleSheet('color:#ff8f8f;font-size:12px;')
        self.lb_status.setText(('识别失败：' + msg))
        self.overlay.set_values(self.overlay.rows[0][1], '—', '—', '—', msg[:40], bad=True)
        self.overlay.set_status('error', '计算失败（接口/网络）')

    def set_mode(self, m):
        """间瞄火力支援 / 侦察报告 / 发布命令：三套键位与截图框互不通用。"""
        m = m if m in ('arty', 'recon', 'command', 'barrage') else 'arty'
        self.mode = m
        self.cfg['mode'] = m
        save_config(self.cfg)
        self.drone = None
        if self.tel:
            self._close_minimap()
            self.tel = None
            self._set_digit_keys(())
        if self.recon:
            self._close_minimap()
            self.recon = None
            self._set_digit_keys(())
            self._set_char_keys(False)
            self._set_mapnum_keys(False)
        if self.cmd:
            self.cmd = None
            self._set_digit_keys(())
            self._set_char_keys(False)
            self._set_mapnum_keys(False)
        if getattr(self, 'bar', None):
            self._close_minimap()
            self.bar = None
            self._set_digit_keys(())
        self.btn_mode_arty.setChecked(m == 'arty')
        self.btn_mode_recon.setChecked(m == 'recon')
        self.btn_mode_cmd.setChecked(m == 'command')
        self.btn_mode_bar.setChecked(m == 'barrage')
        arty = m == 'arty'
        recon = m == 'recon'
        cmd = m == 'command'
        bar = m == 'barrage'
        self.gb_sub.setVisible(arty)
        self.gb_reg.setVisible((arty and self.arty_sub == 'drone'))
        self.gb_scope.setVisible((arty and self.arty_sub == 'scope'))
        self.btn_sub_drone.setChecked(self.arty_sub == 'drone')
        self.btn_sub_scope.setChecked(self.arty_sub == 'scope')
        rsub = getattr(self, 'recon_sub', 'scope')
        self.btn_rsub_drone.setChecked(rsub == 'drone')
        self.btn_rsub_scope.setChecked(rsub == 'scope')
        self.btn_rsub_map.setChecked(rsub == 'map')
        self.w_rreg_drone.setVisible((recon and rsub == 'drone'))
        self.w_rreg_scope.setVisible((recon and rsub == 'scope'))
        self.w_rreg_map.setVisible((recon and rsub == 'map'))
        self._sync_rsrc_buttons()
        for w, show in ((self.gb_res, arty), (self.gb_recon, recon), (self.gb_recon_res, recon), (self.lb_recon_report, recon), (self.gb_cmd, cmd), (self.gb_bar, bar)):
            w.setVisible(show)
        for w in (self.btn_manual, self.btn_exact, self.btn_reset_pending):
            w.setVisible(arty)
        self._refresh_all_region_combos()
        self.refresh_region_label()
        if hasattr(self, 'overlay'):
            self.overlay.clear_options()
        self.lb_scope.setText(self._scope_hint())
        self.lb_hk.setText(self._hk_hint())
        self.lb_sub.setText(self._sub_text())
        self.lb_tip.setText(self._tip_text())
        if bar:
            self.gun = None
            self.view.gun = None
            self.view.player = None
            self.view.buddy = None
            self.view.grid_level = 4
            self.view.target_title = '起始点'
            self.view.target = None
            self.view.update()
        elif arty:
            self.view.gun = self.gun
            self.view.player = None
            self.view.buddy = None
            self.view.target = self.target
            self.view.grid_level = self.grid_level()
            self.view.target_title = '敌目标' if self.tgt_mode == 'tel' else '目标'
            self.view.update()
        elif recon:
            self.view.gun = None
            self.view.player = None
            self.view.grid_level = 4
            self.view.target_title = '敌目标'
            self.view.target = self.recon_last['target'] if self.recon_last else None
            self.view.buddy = self.recon_last['buddy_pt'] if self.recon_last else None
            self.view.update()
            self.refresh_targets_label()
        else:
            self.view.gun = None
            self.view.player = None
            self.view.buddy = None
            self.view.grid_level = 4
            self.view.target_title = '行动地点'
            self.view.target = None
            self.view.update()
        self._sync_mode_overlay()
        self.lb_sub.setText(self._sub_text())
        self.lb_recon.setText(self._recon_hint())
        self.lb_cmd_step.setText(self._cmd_hint())
        self.lb_cmd_hint.setText(self._cmd_tip())
        if hasattr(self, 'gb_bar'):
            self.refresh_bar_gun_combo()
            self.refresh_bar_positions()
            self.lb_bar_hint.setText(self._bar_hint())
            self.lb_bar_step.setText(self._bar_step_text())
            return None

    def set_arty_sub(self, sub):
        """间瞄的两个子模式：drone=无人机视角（只截目标坐标）/ scope=望远镜（方位角流程）。"""
        sub = 'scope' if sub == 'scope' else 'drone'
        if sub == getattr(self, 'arty_sub', 'drone'):
            self.btn_sub_drone.setChecked(sub == 'drone')
            self.btn_sub_scope.setChecked(sub == 'scope')
            return None
        self.arty_sub = sub
        self.cfg['arty_sub'] = sub
        self.cfg['tel_on'] = sub == 'scope'
        save_config(self.cfg)
        if self.tel:
            self.tel_reset(True, '')
        self.drone = None
        self._close_minimap()
        self.btn_sub_drone.setChecked(sub == 'drone')
        self.btn_sub_scope.setChecked(sub == 'scope')
        arty = self.mode == 'arty'
        self.gb_reg.setVisible((arty and sub == 'drone'))
        self.gb_scope.setVisible((arty and sub == 'scope'))
        self._refresh_all_region_combos()
        self.refresh_region_label()
        self.lb_scope.setText(self._scope_hint())
        self.lb_hk.setText(self._hk_hint())
        self.lb_sub.setText(self._sub_text())
        self.lb_tip.setText(self._tip_text())
        self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
        if sub == 'scope':
            miss = (not (self.cfg.get('s_region_az') and self.cfg.get('s_region')))
            hk = self.hk_texts.get(21, 'F8')
            self.lb_status.setText('望远镜视角：先在面板里框好 ① 玩家坐标 ② 方位角' if miss else f"望远镜视角：切回游戏按 {hk} 读方位角")
        else:
            self.lb_status.setText(f"无人机视角：{self.hk_text} 快速定位 / {self.hk_texts[2]} 准确定位")
        if self.mode == 'arty':
            self.recompute()
            return None

    def recon_az_region(self):
        r = self.cfg.get('r_region_az')
        if r:
            return list(r)

    def recon_drone_az_region(self):
        """侦察·无人机视角要读的方位角框：直接沿用「间瞄火力支援·无人机视角」那两块
    （同一台无人机的 HUD 读数位置一样，不必重复框）。没框就返回 None。"""
        who = 'scout' if getattr(self, 'recon_src', 'cmd') == 'scout' else 'cmd'
        r = self.cfg.get(self.drone_az_key(who))
        if r:
            return list(r)

    def _recon_target_first(self):
        """侦察·无人机视角的两步里，先读目标坐标、后读方位角吗？
    指挥官无人机一直在盘旋、方位角时刻在变 → 先读坐标再读方位角，弹图用的是最新方位；
    侦察兵无人机不盘旋 → 仍按老顺序（先方位角、后坐标）。"""
        return (getattr(self, 'recon_sub', 'scope') == 'drone' and getattr(self, 'recon_src', 'cmd') != 'scout' and bool(self.recon_drone_az_region()))

    def recon_grid_region(self):
        r = self.cfg.get('r_region')
        if r:
            return list(r)

    def _tgt_key(self):
        """当前该用哪台无人机的目标坐标框：指挥官 r_region_tgt / 侦察兵 r_region_tgt2。"""
        if getattr(self, 'recon_src', 'cmd') == 'scout':
            return 'r_region_tgt2'
        return 'r_region_tgt'

    @staticmethod
    def _src_name(src):
        if src == 'scout':
            return '侦察兵无人机'
        return '指挥官无人机'

    def set_recon_src(self, src):
        """切换无人机视角用哪台无人机的截图框（两块区域各自保存，互不覆盖）。"""
        src = 'scout' if src == 'scout' else 'cmd'
        changed = src != getattr(self, 'recon_src', 'cmd')
        self.recon_src = src
        self.cfg['recon_src'] = src
        save_config(self.cfg)
        self._sync_rsrc_buttons()
        self.refresh_region_label()
        if changed and (self.recon or {}).get('state') in ('raz', 'target', 'digit4', 'digit4_ok', 'tgt_mini'):
            self.recon_clear(f"已切到「{self._src_name(src)}」：方位角要重读，没走完的流程清掉了")
            return None
        if changed:
            self._close_minimap()
        self.recon_recompute()

    def _sync_rsrc_buttons(self):
        src = getattr(self, 'recon_src', 'cmd')
        if hasattr(self, 'btn_rsrc_cmd'):
            self.btn_rsrc_cmd.setChecked(src != 'scout')
            self.btn_rsrc_scout.setChecked(src == 'scout')
            return None

    def recon_tgt_region(self):
        r = self.cfg.get(self._tgt_key())
        if r:
            return list(r)

    def _recon_warn(self, msg, short=''):
        self.lb_status.setStyleSheet('color:#ff8f8f;font-size:12px;')
        self.lb_status.setText(msg)
        if short:
            self.overlay.set_status('error', short)
            return None

    def _recon_hint(self):
        hk_mark = self.hk_texts.get(11, 'F5')
        hk_rpt = self.hk_texts.get(14, 'F11')
        hk_clr = self.hk_texts.get(12, 'F6')
        if self.mode != 'recon':
            return f"切到「侦察报告」模式后可用：{hk_mark} 标记 / {hk_rpt} 报告 / {hk_clr} 清除目标"
        sub = getattr(self, 'recon_sub', 'scope')
        if sub == 'drone':
            if self._recon_target_first():
                return f"{hk_mark} 标记（新目标 / 更新已有）→ 截目标坐标 → 读方位角 → 小地图上点真实位置 → 选型号；{hk_rpt} 报告；{hk_clr} 清除目标"
            if self.recon_drone_az_region():
                return f"{hk_mark} 标记（新目标 / 更新已有）→ 读方位角 → 截目标坐标 → 小地图上点真实位置 → 选型号；{hk_rpt} 报告；{hk_clr} 清除目标"
            return f"{hk_mark} 标记（新目标 / 更新已有）→ 截目标坐标 → 补第 4 位 → 再按 {hk_mark} 确认 → 选型号；（框了方位角区域后，会多一步读方位角并弹 60 m 小地图）；{hk_rpt} 报告；{hk_clr} 清除目标"
        if sub == 'map':
            return f"{hk_mark} 标记（新目标 / 更新已有）→ 键入 4 级格号 → 再按 {hk_mark} 确认 → 选型号；{hk_rpt} 报告；{hk_clr} 清除目标"
        return f"{hk_mark} 标记（新目标 / 更新已有）：方位角 → 坐标 → 距离 → 小地图上点真实位置（距离 > 100 m 才弹）→ 选型号；{hk_rpt} 报告；{hk_clr} 清除目标"

    def set_recon_sub(self, sub):
        sub = sub if sub in ('drone', 'scope', 'map') else 'scope'
        if sub == getattr(self, 'recon_sub', ''):
            self.btn_rsub_drone.setChecked(sub == 'drone')
            self.btn_rsub_scope.setChecked(sub == 'scope')
            self.btn_rsub_map.setChecked(sub == 'map')
            return None
        self.recon_sub = sub
        self.cfg['recon_sub'] = sub
        save_config(self.cfg)
        self.recon_clear('已切换侦察子模式（没走完的流程清掉了）')
        self.btn_rsub_drone.setChecked(sub == 'drone')
        self.btn_rsub_scope.setChecked(sub == 'scope')
        self.btn_rsub_map.setChecked(sub == 'map')
        self.w_rreg_drone.setVisible(sub == 'drone')
        self.w_rreg_scope.setVisible(sub == 'scope')
        self.w_rreg_map.setVisible(sub == 'map')
        self.refresh_region_label()
        self.lb_recon.setText(self._recon_hint())
        self.lb_hk.setText(self._hk_hint())
        if self.mode == 'recon':
            self.recon_recompute()
        if sub == 'drone':
            self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
            self.lb_status.setText('无人机视角：框好「敌目标坐标区域」后，按标记键截目标')
            return None
        if sub == 'map':
            self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
            self.lb_status.setText('地图视角：不用框区域，按标记键后直接键入目标的 4 级格号（如 K12-1-8-8）')
            return None
        self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
        self.lb_status.setText('望远镜视角：框好方位角 / 玩家坐标两块后，按标记键开始')

    def _faction_data(self):
        fid = (self.cfg.get('r_faction') or '')
        if not fid:
            return None
        for f in ((self.recon_targets or {}).get('factions') or []):
            if not f.get('id') == fid:
                continue
            return f

    def _refresh_faction_combo(self):
        cur = (self.cfg.get('r_faction') or '')
        self.cb_faction.blockSignals(True)
        self.cb_faction.clear()
        self.cb_faction.addItem('（先选敌方阵营）', '')
        idx = 0
        for i, f in enumerate(((self.recon_targets or {}).get('factions') or [])):
            self.cb_faction.addItem((f.get('label') or f.get('name') or f.get('id')), f.get('id'))
            if not f.get('id') == cur:
                continue
            idx = i + 1
        self.cb_faction.setCurrentIndex(idx)
        self.cb_faction.blockSignals(False)

    def on_faction_changed(self, idx):
        fid = (self.cb_faction.itemData(idx) or '')
        self.cfg['r_faction'] = fid
        save_config(self.cfg)
        f = self._faction_data()
        self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
        if f:
            n = sum((len((c.get('items') or [])) for c in (f.get('cats') or [])))
            self.lb_status.setText(f"敌方阵营：{f.get('label')}（{len((f.get('cats') or []))} 类 / {n} 个型号）")
            if (self.recon or {}).get('state') == 'type':
                if (self.recon or {}).get('grid'):
                    self.recon_recompute()
                    return None
                return None
            return None
        self.lb_status.setText('还没选敌方阵营：标记目标前要先选一个')

    def refresh_targets_label(self):
        n = 0
        if getattr(self, 'target_db', None) is not None:
            try:
                n = self.target_db.count(self.map.slug)
            except Exception:
                n = 0
        hk = self.hk_texts.get(14, 'F11')
        if n:
            self.lb_targets.setText(f"目标库：本图 {n} 条 · 按 {hk} 报告")
            return None
        self.lb_targets.setText('目标库：本图还没有记录')

    def clear_targets(self):
        if getattr(self, 'target_db', None) is None:
            self._recon_warn('目标库打不开（targets.db）')
            return None
        n = self.target_db.count(self.map.slug)
        if not n:
            self.lb_status.setText('目标库里还没有这张图的记录')
            return None
        r = QMessageBox.question(self, '清空目标库', f"删掉这张图上的 {n} 条目标记录？（不能撤销）", (QMessageBox.Yes | QMessageBox.No), QMessageBox.No)
        if r != QMessageBox.Yes:
            return None
        gone = self.target_db.clear(self.map.slug)
        self.refresh_targets_label()
        self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
        self.lb_status.setText(f"已清空本图目标库（{gone} 条）")

    def recon_clear(self, msg='已清除侦察流程（目标库里的记录保留）'):
        had = self.recon is not None
        self.recon = None
        self._close_minimap()
        self._set_digit_keys(())
        self._set_char_keys(False)
        self._set_mapnum_keys(False)
        self.overlay.clear_options()
        self.view.player = None
        self.view.buddy = self.recon_last['buddy_pt'] if self.recon_last else None
        self.view.target = self.recon_last['target'] if self.recon_last else None
        self.view.update()
        if self.mode == 'recon':
            self.recon_recompute()
        if had:
            self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
            self.lb_status.setText(msg)
            return None

    def recon_step(self):
        if self.mode != 'recon':
            self._recon_warn('当前不是侦察报告模式：先切到「侦察报告」再用这个键')
            return None
        st = (self.recon or {}).get('state')
        if st in (None, 'done', 'saved'):
            self.mark_menu()
            return None
        if st in ('mark_mode', 'upd_pick'):
            self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
            self.lb_status.setText('现在等你按数字键在浮窗里选（0 = 取消）')
            return None
        if st == 'az':
            self.recon_capture_az()
            return None
        if st == 'raz':
            self.recon_capture_drone_az(first=(not (self.recon or {}).get('wait_az')))
            return None
        if st == 'pos':
            self.recon_capture_pos()
            return None
        if st == 'target':
            self.recon_capture_tgt()
            return None
        if st == 'digit':
            self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
            self.lb_status.setText('现在等你按小键盘 1–9 补全坐标最后一位')
            return None
        if st == 'digit4':
            self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
            self.lb_status.setText('现在等你按小键盘 1–9 补目标坐标最后一位')
            return None
        if st == 'digit4_ok':
            self._commit_digit4()
            return None
        if st == 'tgt_mini':
            self._recon_tgt_mini_cancel()
            self.recon_capture_tgt()
            return None
        if st == 'mcoord':
            self._commit_mapcoord()
            return None
        if st == 'dist':
            self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
            self.lb_status.setText(f"现在等你在小键盘输 3 位距离（已输 {len((self.recon.get('digits') or ''))}/3）")
            return None
        if st in ('type', 'item', 'common'):
            self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
            self.lb_status.setText('现在等你按数字键在浮窗里选「这是什么敌方目标」')
            return None
        if st == 'upd_mini':
            self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
            self.lb_status.setText('现在等你在小地图上点一下目标的真实位置（点一下才更新这条记录；按退格取消）')
            return None
        if st == 'rpt_pick':
            self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
            self.lb_status.setText('现在等你按数字键在浮窗里选要报告的目标')
            return None
        if st == 'rpt_buddy':
            self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
            self.lb_status.setText(f"现在等你输入队友的 4 级坐标（已输 {len((self.recon.get('buf') or ''))}/6）")
            return None

    def _map_rows(self):
        if getattr(self, 'target_db', None) is None:
            return []
        try:
            return self.target_db.list_map(self.map.slug)
        except Exception as e:
            print('读目标库失败:', e)
            return []

    def mark_menu(self):
        """按标记键：先问是「新目标」还是「把某个已有目标挪到新位置」。"""
        rows = self._map_rows()
        self._set_char_keys(False)
        self._set_digit_keys('0123456789')
        self.recon = {'state': 'mark_mode', 'n': len(rows), 'page': 0}
        self._recon_mini_drop()
        self.recon_recompute()
        self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
        if rows:
            self.lb_status.setText('浮窗里选：1 标记新目标（分配新 UID）· 2 更新已有目标的位置')
            return None
        self.lb_status.setText('浮窗里按 1 标记新目标（目标库还空着，没有可更新的）')

    def _start_capture(self, upd=None):
        """真正开始截坐标；upd=已有目标那一行（更新位置），None=新目标。"""
        sub = getattr(self, 'recon_sub', 'scope')
        base = {'target': None, 'grid': '', 'page': 0, 'cat': ''}
        if upd:
            base.update({'upd_tid': int((upd.get('id') or 0)), 'upd_uid': (upd.get('uid') or ''), 'upd_name': (upd.get('name') or '敌对目标')})
        if sub == 'map':
            self.recon = dict(base, state='mcoord', buf='')
            self.view.player = None
            self._set_digit_keys('0123456789')
            self._set_char_keys(True)
            self._set_mapnum_keys(True)
            self.recon_recompute()
            self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
            self.lb_status.setText(f"键入敌目标的 4 级格号（字母 + 行号 + 三位子格，如 K12-1-8-8）：已输 0/6；输完按 {self.hk_texts.get(11, 'F5')} 确认，退格可删一位")
            return None
        if sub == 'drone':
            if not self.recon_tgt_region():
                self._recon_warn('先在「敌目标坐标区域」框选一块（无人机视角用），再按标记键', '没设目标坐标区域 · 面板里框一下')
                self.mark_menu()
                return None
            if self._recon_target_first():
                self.recon = dict(base, state='target', pos_txt='')
                self.recon_capture_tgt()
                return None
            if self.recon_drone_az_region():
                self.recon = dict(base, state='raz', az=None, pos_txt='')
                self.recon_capture_drone_az()
                return None
            self.recon = dict(base, state='target', pos_txt='')
            self.recon_capture_tgt()
            return None
        if not self.recon_az_region():
            self._recon_warn('先在「侦察报告 ①」框选方位角区域，再按标记键', '没设侦察方位角区域 · 面板里框一下')
            self.mark_menu()
            return None
        if not self.recon_grid_region():
            self._recon_warn('先在「侦察报告 ②」框选玩家坐标区域，再按标记键', '没设侦察坐标区域 · 面板里框一下')
            self.mark_menu()
            return None
        self.recon = dict(base, state='az', az=None, pos=None, pos_txt='', digits='', buf='')
        self.recon_capture_az()

    def recon_capture_az(self):
        self.ocr_what = ''
        self._ocr_job(self.recon_az_region(), 'az', '侦察：识别方位角（1/3）', '把准星对着目标，读你视角的方位角', 'recon')

    def recon_capture_pos(self):
        self.ocr_what = ''
        self._ocr_job(self.recon_grid_region(), 'pos', '侦察：识别你的坐标（2/3）', '读 CURRENT GRID /「玩家位置：」', 'recon')

    def recon_capture_drone_az(self, first=True):
        """侦察·无人机视角读无人机 HUD 上的方位角（大字 + 粗黑描边，
    现有本地模板库对不上，所以跳过本地、也不往库里学，只走 AI）。
    first=True 老顺序的第 1 步（侦察兵无人机，读完再截坐标）；
    first=False 新顺序的第 2 步（指挥官无人机，坐标已读，读完直接弹小地图）。"""
        self.ocr_what = ''
        src = self._src_name(getattr(self, 'recon_src', 'cmd'))
        if not self.ed_key.text().strip():
            self._recon_warn('无人机方位角读不了本地模板、只能走 AI —— 先在面板填 DeepSeek 密钥（或者不要框方位角框，就退回一次读完的老流程）', '无人机方位角要 AI · 先填密钥')
            return None
        region = self.recon_drone_az_region()
        if not region:
            cur = self.recon or {}
            wait = cur.pop('wait_az', None) if cur else None
            self._recon_warn(f"「{src}」的方位角框没了（面板里被清掉）—— 这次不读方位角，直接按坐标往下走", '方位角框没了 · 跳过这一步')
            if wait == 'tgt4':
                self._after_target((cur.get('grid') or cur.get('pos_txt') or ''), 'drone')
                return None
            if wait == 'tgt3':
                self._recon_tgt3_mini((cur.get('pos_txt') or ''), (cur.get('target') or (0.0, 0.0)))
                return None
            if cur:
                cur['state'] = 'target'
                cur['pos_txt'] = ''
                self.recon_recompute()
            return None
        self._ocr_job(region, 'az', f"侦察：识别{src}的方位角（{'1/2' if first else '2/2'}）", f"读无人机画面上的方位读数（{src}）—— 它决定小地图的正上方" if first else f"目标坐标已读，再读一次{src}的方位角 —— 它一直在转，小地图要用最新的一次", 'recon', local=False, learn=False)

    def recon_capture_tgt(self):
        self.ocr_what = 'r_tgt'
        src = self._src_name(getattr(self, 'recon_src', 'cmd'))
        region = self.recon_tgt_region()
        if not region:
            self._recon_warn(f"还没框选「{src}」的目标坐标区域 —— 面板「敌目标坐标区域」里点「框选目标坐标」框一下", f"没设{src}的框 · 先框一下")
            return None
        self._ocr_job(region, 'pos', f"侦察：识别敌目标坐标（{src}）", f"把准星对准敌方目标（{src}）", 'recon')

    def on_recon_ok(self, coord, raw, ms):
        if getattr(self, 'ocr_what', '') == 'r_tgt':
            self.recon_tgt_ok(coord, raw, ms)
            return None
        if self.ocr_kind == 'az':
            self.recon_az_ok(coord, raw, ms)
            return None
        self.recon_pos_ok(coord, raw, ms)

    def recon_az_ok(self, coord, raw, ms):
        if not coord:
            self._recon_warn(f"没读到方位角（模型说：{raw[:40]}）—— 再按 {self.hk_texts[11]} 重试", '没读到方位角 · 再截一次')
            return None
        try:
            az = float(coord) % 360.0
        except ValueError:
            self._recon_warn('方位角不合法 · 再截一次')
            return None
        self.recon['az'] = az
        src = '本地识别' if raw == 'local' else f"AI 识别 {ms} ms"
        if (self.recon or {}).get('state') == 'raz':
            cur = self.recon
            wait = cur.pop('wait_az', None)
            if wait:
                if wait == 'tgt4':
                    self._after_target(cur.get('grid') or cur.get('pos_txt') or '', 'drone')
                    return None
                self._recon_tgt3_mini(cur.get('pos_txt') or '', cur.get('target') or (0.0, 0.0))
                return None
            cur['state'] = 'target'
            cur['pos_txt'] = ''
            self.recon_recompute()
            self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
            self.lb_status.setText(f"方位角 {az:.0f}° · {src} —— 再按 {self.hk_texts[11]} 截敌目标坐标（2/2），读完会弹 60 m 小地图")
            return None
        self.recon['state'] = 'pos'
        self.recon_recompute()
        self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
        self.lb_status.setText(f"方位角 {az:.0f}° · {src} —— 再按 {self.hk_texts[11]} 截你自己的位置坐标（2/3）")

    def recon_pos_ok(self, coord, raw, ms):
        if not coord:
            self._recon_warn(f"没读到坐标（模型说：{raw[:40]}）—— 再按 {self.hk_texts[11]} 重试", '没读到坐标 · 再截一次')
            return None
        try:
            rx, rz = core.parse(coord)
        except core.ParseError as e:
            self._recon_warn(str(e))
            return None
        else:
            if rx > (self.map.spanx + 1) or rz > (self.map.spanz + 1):
                self._recon_warn(f"{coord} 不在这张地图上")
                return None
            self.recon['pos'] = (min(rx, self.map.spanx), min(rz, self.map.spanz))
            self.recon['pos_txt'] = coord
            self.view.player = self.recon['pos']
            self.view.update()
            if len(coord.split('-')) >= 4:
                self.recon['state'] = 'dist'
                self.recon['digits'] = ''
                self._set_digit_keys('0123456789')
                self.recon_recompute()
                self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
                self.lb_status.setText(f"位置 {coord} —— 用小键盘输你到目标的距离（3 位，如 155 / 090 / 001）；按错可按退格删一位")
                return None
            self.recon['state'] = 'digit'
            self._set_digit_keys('123456789')
            self.recon_recompute()
            self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
            self.lb_status.setText(f"位置 {coord} —— 按小键盘 1–9 补全坐标最后一位（{coord}-?，即 3.7 m 格）")

    def recon_tgt_ok(self, coord, raw, ms):
        self.ocr_what = ''
        if not coord:
            self._recon_warn(f"没读到坐标（模型说：{raw[:40]}）—— 再按 {self.hk_texts[11]} 重试", '没读到坐标 · 再截一次')
            return None
        try:
            rx, rz = core.parse(coord)
        except core.ParseError as e:
            self._recon_warn(str(e))
            return None
        else:
            if rx > (self.map.spanx + 1) or rz > (self.map.spanz + 1):
                self._recon_warn(f"{coord} 不在这张地图上")
                return None
            self.recon['pos_txt'] = coord
            pt = (min(rx, self.map.spanx), min(rz, self.map.spanz))
            four = len(coord.split('-')) >= 4
            if self._recon_target_first() and self.recon.get('az') is None:
                cur = self.recon
                cur['target'] = pt
                cur['grid'] = coord if four else ''
                cur['wait_az'] = 'tgt4' if four else 'tgt3'
                cur['state'] = 'raz'
                self._set_digit_keys(())
                self._set_char_keys(False)
                self.view.target = pt
                self.view.target_title = '敌目标（估）'
                self.view.update()
                self.recon_recompute()
                src = self._src_name(getattr(self, 'recon_src', 'cmd'))
                self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
                self.lb_status.setText(f"敌目标 {coord} —— 接着读一次{src}的方位角（2/2，它一直在转，要用最新的），读完就弹 {self.DRONE_SPAN:g} m 小地图")
                self.recon_capture_drone_az(first=False)
                return None
            if four:
                self.recon['target'] = pt
                self._after_target(coord, 'drone')
                return None
            self._recon_tgt3_mini(coord, pt)

    def _recon_tgt3_mini(self, coord, pt):
        """只读到 3 级坐标（如 B11-2-5）时不再让人手补第 4 位 —— 直接以这个 3 级坐标的
    格心为心弹 60 m 小地图，玩家点一下目标的真实位置；4 级坐标只用来显示，
    目标记录与后续计算都用点出来的原始坐标。（侦察·无人机与更新已有目标共用）"""
        cur = self.recon
        cur['target'] = pt
        cur['grid'] = ''
        cur['state'] = 'tgt_mini'
        self._set_digit_keys(())
        self._set_char_keys(False)
        self.view.target = pt
        self.view.target_title = '敌目标（估）'
        self.view.update()
        cur['mini'] = True
        if self._open_minimap(cur['target'], cur.get('az'), None, span=self.DRONE_SPAN):
            self.recon_recompute()
            self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
            self.lb_status.setText(f"敌目标 {coord}（3 级坐标）—— 小地图已弹出：点一下目标的真实位置；找不到就按退格取消，再按 {self.hk_texts.get(11, 'F5')} 重截坐标")
            return None
        cur.pop('mini', None)
        cur['target'] = None
        cur['state'] = 'digit4'
        self._set_digit_keys('123456789')
        self.recon_recompute()
        self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
        self.lb_status.setText(f"敌目标 {coord} —— 按小键盘 1–9 补最后一位（{coord}-?，即 3.7 m 格），补完按 {self.hk_texts.get(11, 'F5')} 确认")

    def recon_digit(self, d):
        st = (self.recon or {}).get('state')
        if st == 'digit':
            if d == '0':
                return None
            txt = f"{self.recon['pos_txt']}-{d}"
            try:
                rx, rz = core.parse(txt)
            except core.ParseError as e:
                self._recon_warn(str(e))
                return None
            self.recon['pos'] = (min(rx, self.map.spanx), min(rz, self.map.spanz))
            self.recon['pos_txt'] = txt
            self.recon['manual4'] = True
            self.view.player = self.recon['pos']
            self.recon['state'] = 'dist'
            self.recon['digits'] = ''
            self._set_digit_keys('0123456789')
            self.recon_recompute()
            self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
            self.lb_status.setText(f"位置 {txt} —— 用小键盘输你到目标的距离（3 位：百→个，如 155 / 090 / 001）；按错可按退格删一位")
            return None
        if st in ('digit4', 'digit4_ok'):
            if d == '0':
                return None
            txt = f"{self.recon.get('pos_txt')}-{d}"
            try:
                core.parse(txt)
            except core.ParseError as e:
                self._recon_warn(str(e))
                return None
            self.recon['pend4'] = d
            self.recon['state'] = 'digit4_ok'
            self._set_digit_keys('123456789')
            self.recon_recompute()
            self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
            self.lb_status.setText(f"敌目标 {txt} —— 按 {self.hk_texts.get(11, 'F5')} 确认入库；敲错了按退格重输（或直接敲别的数字）")
            return None
        if st == 'dist':
            self.recon['digits'] = (self.recon.get('digits') or '') + d
            n = len(self.recon['digits'])
            if n < 3:
                self.recon_recompute()
                self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
                self.lb_status.setText(f"距离 {self.recon['digits']}… 再输 {(3 - n)} 位（共 3 位）")
                return None
            self.recon_target_ready()
            return None
        if st == 'mcoord':
            self.recon_map_char(d)
            return None
        if st in ('type', 'item', 'common', 'rpt_pick'):
            self.recon_pick_digit(d)
            return None
        if st in ('mark_mode', 'upd_pick', 'clr_choose', 'clr_pick'):
            self._flow_digit(d)
            return None
        return None

    def recon_char(self, ch):
        """字母 / 小键盘数字：地图视角敲格号走这里，其余（队友坐标）照旧走 recon_buddy_char。"""
        if (self.recon or {}).get('state') == 'mcoord':
            self.recon_map_char(ch)
            return None
        self.recon_buddy_char(ch)

    def recon_map_char(self, ch):
        """地图视角：键入 4 级格号（字母 + 2 位行号 + 3 位子格，共 6 个字符）。"""
        if (self.recon or {}).get('state') != 'mcoord':
            return None
        buf = (self.recon.get('buf') or '')
        n = len(buf)
        hk_mark = self.hk_texts.get(11, 'F5')
        if n >= 6:
            self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
            self.lb_status.setText(f"格号已经输满 6 位（{self._recon_buf_text(buf)}）—— 按 {hk_mark} 确认；要改就按退格删一位")
            return None
        if n == 0:
            if not ch.isalpha():
                self._recon_warn('格号第 1 位必须是字母（如 K、B、N）')
                return None
        elif n < 3:
            if not ch.isdigit():
                self._recon_warn('格号第 2–3 位是行号数字（K7 要打成 K07）')
                return None
        elif (not ch.isdigit() or ch == '0'):
            self._recon_warn('格号最后三位各是 1–9（子格位）')
            return None
        buf += ch.upper()
        self.recon['buf'] = buf
        shown = self._recon_buf_text(buf)
        if len(buf) < 6:
            self.recon_recompute()
            self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
            self.lb_status.setText(f"格号 {shown} · 再输 {(6 - len(buf))} 位（共 6 位，例 K12-1-8-8）")
            return None
        self.recon_recompute()
        self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
        self.lb_status.setText(f"格号 {shown} —— 按 {hk_mark} 确认；敲错了按退格删一位")

    def _commit_mapcoord(self):
        """地图视角：敲满 6 位格号后按标记键确认，才真正入库。"""
        cur = self.recon or {}
        buf = (cur.get('buf') or '')
        hk_mark = self.hk_texts.get(11, 'F5')
        if len(buf) != 6:
            self._recon_warn(f"格号还没输完（{len(buf)}/6）—— 输满 6 位再按 {hk_mark} 确认", f"还差 {(6 - len(buf))} 位 · 输完再按 {hk_mark}")
            return None
        text = self._recon_buf_text(buf)
        try:
            rx, rz = core.parse(text)
        except core.ParseError as e:
            self._recon_warn(str(e))
            return None
        else:
            if rx > (self.map.spanx + 1) or rz > (self.map.spanz + 1):
                self._recon_warn(f"{text} 不在这张地图上（图幅 {self.map.spanx:.0f}×{self.map.spanz:.0f} m）", '坐标不在这张图上 · 退格改一下')
                return None
            cur['target'] = (min(rx, self.map.spanx), min(rz, self.map.spanz))
            cur['buf'] = ''
            self._set_char_keys(False)
            self._set_mapnum_keys(False)
            self._after_target(text, 'map')

    def _commit_digit4(self):
        """无人机视角手补的第 4 位：按标记键确认后才真正入库。"""
        cur = self.recon or {}
        txt = f"{(cur.get('pos_txt') or '')}-{(cur.get('pend4') or '')}"
        try:
            rx, rz = core.parse(txt)
        except core.ParseError as e:
            self._recon_warn(str(e))
            return None
        else:
            cur.pop('pend4', None)
            cur['target'] = (min(rx, self.map.spanx), min(rz, self.map.spanz))
            self._after_target(txt, 'drone')

    def recon_target_ready(self):
        digits = (self.recon.get('digits') or '')
        if len(digits) != 3:
            return None
        dist = float(int(digits))
        az = float((self.recon.get('az') or 0.0))
        pos = self.recon.get('pos')
        if pos is None:
            self.recon_clear('侦察流程缺位置，已重置')
            return None
        rad = math.radians(az)
        tx = (pos[0] + (dist * math.sin(rad)))
        tz = (pos[1] - (dist * math.cos(rad)))
        tx = max(0.0, min(tx, self.map.spanx))
        tz = max(0.0, min(tz, self.map.spanz))
        self.recon['dist'] = dist
        self.recon['target'] = (tx, tz)
        self.view.player = pos
        self._after_target(self.map.grid_at(tx, tz, 4), 'scope')

    def _after_target(self, grid_txt, src):
        """目标坐标确定了：把界面切到「这是什么敌方目标？」的选择态。"""
        cur = self.recon or {}
        self._set_mapnum_keys(False)
        if cur.get('upd_tid'):
            cur['grid'] = grid_txt
            cur['src'] = src
            self._set_digit_keys(())
            self._set_char_keys(False)
            self.view.target = cur.get('target')
            self.view.target_title = '敌目标'
            self.view.update()
            if self._maybe_recon_minimap(src):
                cur['state'] = 'upd_mini'
                self.recon_recompute()
                self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
                self.lb_status.setText(f"目标 {(cur.get('grid') or '')}（估测）—— 小地图已弹出：点一下目标的真实位置就更新这条记录；不想改就按退格（取消，记录不动）")
                return None
            self._finish_update(grid_txt)
            return None
        if not self._faction_data():
            self._set_digit_keys(())
            self._recon_warn('先在面板「敌方阵营」里选好敌人是谁，再标记目标', '没选敌方阵营 · 面板里选一下')
            self.recon['state'] = 'type'
            self.recon_recompute()
            self.overlay.set_options('还没选敌方阵营', [('0', '先只标记位置（不入库）')], '在面板「敌方阵营」里选一个，再来标目标')
            self.overlay.set_status('error', '没选敌方阵营 · 面板里选一下')
            return None
        self.recon['state'] = 'type'
        self.recon['grid'] = grid_txt
        self.recon['src'] = src
        self.recon['page'] = 0
        self.recon['cat'] = ''
        self.view.target = self.recon['target']
        self.view.target_title = '敌目标'
        self.view.update()
        self._set_char_keys(False)
        self._set_digit_keys('0123456789')
        self.recon_recompute()
        self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
        self.lb_status.setText(f"目标 {grid_txt} —— 在浮窗里按数字选「这是什么敌方目标」（0 = 只标记位置，不入库）")
        self._maybe_recon_minimap(src)

    def _maybe_recon_minimap(self, src):
        """目标刚定型时按间瞄那套弹一块小地图（望远镜 120 m / 无人机 60 m，
    图的正上方 = 方位角）。玩家点一下就能把目标挪到真实位置；返回是否弹出来了。
    「标记新目标」与「更新已有目标的位置」两条路都用它（1.8.10 起）；
    地图视角（src=="map"）没有方位角，直接返回 False。"""
        cur = self.recon or {}
        tgt = cur.get('target')
        az = cur.get('az')
        if cur.get('mini_used'):
            return False
        if not tgt or az is None:
            return False
        if src == 'drone':
            span, me = self.DRONE_SPAN, None
        elif src == 'scope':
            dist = (cur.get('dist') or 0.0)
            if dist <= self.MINIMAP_OVER:
                return False
            span, me = None, cur.get('pos')
        else:
            return False
        cur['mini'] = True
        if not self._open_minimap(tgt, float(az), me, span=span, north_key=src == 'scope'):
            cur.pop('mini', None)
            return False
        grid = (cur.get('grid') or self.map.grid_at(*tgt, 4))
        self.overlay.set_note('小地图上点目标的真实位置（点完 1 秒自动收起）')
        self.overlay.set_status('done', f"点小地图修正目标 · {grid}"[:40])
        self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
        self.lb_status.setText(f"目标 {grid}（估测）—— 小地图已弹出：点一下目标的真实位置，点完再在浮窗里按数字选型号；找不到就按退格重来")
        return True

    def _cat_of(self, key):
        f = self._faction_data()
        for c in ((f or {}).get('cats') or []):
            if not c.get('key') == key:
                continue
            return c

    def recon_pick_digit(self, d):
        st = (self.recon or {}).get('state')
        if st == 'type':
            if d == '0':
                self._finish_mark(None)
                return None
            if d == '9':
                self.recon['state'] = 'common'
                self.recon_recompute()
                self.lb_status.setText('浮窗里按数字选通用目标（0 = 返回上级）')
                return None
            cats = ((self._faction_data() or {}).get('cats') or [])
            i = (int(d) - 1)
            if i >= len(cats):
                return None
            self.recon['cat'] = cats[i].get('key')
            self.recon['state'] = 'item'
            self.recon['page'] = 0
            self.recon_recompute()
            self.lb_status.setText(f"浮窗里按数字选型号：{cats[i].get('label')}（9 = 下一页，0 = 返回分类）" if len((cats[i].get('items') or [])) > 8 else f"浮窗里按数字选型号：{cats[i].get('label')}（0 = 返回分类）")
            return None
        if st == 'item':
            cat = self._cat_of(self.recon.get('cat'))
            if cat is None:
                return None
            items = (cat.get('items') or [])
            page = int((self.recon.get('page') or 0))
            start = page * 8
            chunk = items[start:(start + 8)]
            if d == '0':
                self.recon['state'] = 'type'
                self.recon['page'] = 0
                self.recon_recompute()
                return None
            if d == '9':
                if (start + 8) < len(items):
                    self.recon['page'] = (page + 1)
                    self.recon_recompute()
                return None
            i = (int(d) - 1)
            if i >= len(chunk):
                return None
            it = chunk[i]
            self._finish_mark(((cat.get('label') or cat.get('key')), (it.get('name') or '?'), int((it.get('hp') or 0)), int((it.get('ticket') or 0)), (it.get('type') or cat.get('key'))))
            return None
        if st == 'common':
            commons = (((self.recon_targets or {}).get('common') or {}).get('items') or [])
            if d == '0':
                self.recon['state'] = 'type'
                self.recon_recompute()
                return None
            i = (int(d) - 1)
            if i >= len(commons):
                return None
            it = commons[i]
            self._finish_mark(('通用', (it.get('label') or it.get('key')), 0, 0, it.get('key')))
            return None
        if st == 'rpt_pick':
            if d == '0':
                self.recon_clear('已取消这次报告')
                return None
            rows = (self.recon.get('rows') or [])
            page = int((self.recon.get('page') or 0))
            if d == '9':
                if ((page * 8) + 8) < len(rows):
                    self.recon['page'] = (page + 1)
                    self.recon_recompute()
                return None
            idx = (((page * 8) + int(d)) - 1)
            if idx >= len(rows):
                return None
            row = rows[idx]
            self.recon = {'state': 'rpt_buddy', 'row': row, 'buf': '', 'target': (row.get('rx'), row.get('rz')), 'grid': (row.get('grid') or ''), 'name': (row.get('name') or '')}
            self._recon_mini_drop()
            self._set_digit_keys(())
            self._set_char_keys(True)
            self.view.target = self.recon['target']
            self.view.target_title = (row.get('name') or '敌目标')
            self.view.buddy = None
            self.view.update()
            self.recon_recompute()
            self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
            self.lb_status.setText('输入队友的 4 级坐标：字母 + 2 位数字 + 3 位子格（例 B11-2-5-6 直接连打 B 1 1 2 5 6；打错按退格删一位）')
            return None

    def recon_clear_menu(self):
        """清除键：先问清「选哪些目标」，真清之前把消灭通报复制到剪贴板。"""
        if self.mode != 'recon':
            self._recon_warn('当前不是侦察报告模式：先切到「侦察报告」再用这个键')
            return None
        st = (self.recon or {}).get('state')
        if st not in (None, 'done', 'saved', 'mark_mode'):
            hk = self.hk_texts.get(12, 'F6')
            self.recon_clear(f"已取消这次标记（目标库没动）—— 再按 {hk} 可以清除目标")
            return None
        rows = self._map_rows()
        if not rows:
            self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
            self.lb_status.setText('目标库是空的：没有要清除的目标')
            return None
        self._set_char_keys(False)
        self._set_digit_keys('0123456789')
        self.recon = {'state': 'clr_choose', 'rows': rows, 'sel': [], 'page': 0}
        self._recon_mini_drop()
        self.recon_recompute()
        self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
        self.lb_status.setText('浮窗里选：1 选择目标清除 · 2 全部清除 · 0 取消')

    def _flow_digit(self, d):
        """「标记方式 / 选更新目标 / 清除目标」三个菜单里的数字键。"""
        st = (self.recon or {}).get('state')
        cur = self.recon or {}
        if st == 'mark_mode':
            if d == '1':
                self._start_capture()
                return None
            if d == '2':
                rows = self._map_rows()
                if not rows:
                    self._recon_warn('目标库是空的：没有可更新的目标')
                    return None
                self.recon = {'state': 'upd_pick', 'rows': rows, 'page': 0}
                self._recon_mini_drop()
                self.recon_recompute()
                self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
                self.lb_status.setText('浮窗里选要更新位置的目标（9 下一页 · 0 取消）')
                return None
            if d == '0':
                self.recon_clear('已取消标记')
            return None
        if st == 'upd_pick':
            rows = (cur.get('rows') or [])
            page = int((cur.get('page') or 0))
            if d == '0':
                self.recon_clear('已取消标记')
                return None
            if d == '9':
                if ((page * 8) + 8) < len(rows):
                    cur['page'] = (page + 1)
                    self.recon_recompute()
                return None
            idx = (((page * 8) + int(d)) - 1)
            if idx >= len(rows):
                return None
            row = rows[idx]
            self._start_capture(row)
            self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
            self.lb_status.setText(f"更新「{(row.get('uid') or '无 UID')} {(row.get('name') or '目标')}」的位置：按原来的流程重新截坐标")
            return None
        if st == 'clr_choose':
            if d == '1':
                self.recon = {'state': 'clr_pick', 'rows': (cur.get('rows') or []), 'sel': [], 'page': 0}
                self._recon_mini_drop()
                self.recon_recompute()
                self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
                self.lb_status.setText('按 1–8 选中/取消目标，0 = 确认清除（会先复制消灭通报）')
                return None
            if d == '2':
                self._confirm_clear_all()
                return None
            if d == '0':
                self.recon_clear('已取消清除')
            return None
        if st == 'clr_pick':
            rows = (cur.get('rows') or [])
            sel = (cur.get('sel') or [])
            page = int((cur.get('page') or 0))
            if d == '0':
                if not sel:
                    self._recon_warn('还没选目标：按 1–8 选中要清除的，再按 0 确认')
                    return None
                self._clear_selected()
                return None
            if d == '9':
                if ((page * 8) + 8) < len(rows):
                    cur['page'] = (page + 1)
                    self.recon_recompute()
                return None
            idx = (((page * 8) + int(d)) - 1)
            if idx >= len(rows):
                return None
            tid = int((rows[idx].get('id') or 0))
            if tid in sel:
                sel.remove(tid)
            else:
                sel.append(tid)
            cur['sel'] = sel
            self.recon_recompute()
            self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
            self.lb_status.setText(f"已选 {len(sel)} 个（1–8 选中/取消 · 9 下一页 · 0 确认清除）")
            return None

    def _confirm_clear_all(self):
        rows = ((self.recon or {}).get('rows') or [])
        if not rows:
            self._recon_warn('目标库是空的')
            return None
        r = QMessageBox.question(self, '清除全部目标', f"把这张图上的 {len(rows)} 个目标全部清除？（消灭通报会先复制到剪贴板）", (QMessageBox.Yes | QMessageBox.No), QMessageBox.No)
        if r != QMessageBox.Yes:
            return None
        self._clear_rows(rows)

    def _clear_selected(self):
        rows = ((self.recon or {}).get('rows') or [])
        sel = {int(x) for x in ((self.recon or {}).get('sel') or [])}
        picked = [r for r in rows if int((r.get('id') or 0)) in sel]
        if not picked:
            self._recon_warn('还没选目标')
            return None
        self._clear_rows(picked)
        return None

    def _clear_rows(self, rows):
        """真清之前先复制「名字 UID 已经被消灭；…」，再删记录。"""
        lines = ''.join((f"{(r.get('name') or '目标')} {(r.get('uid') or '')} 已经被消灭；".replace('  ', ' ') for r in rows))
        ok = set_clipboard_text(lines)
        gone = 0
        if getattr(self, 'target_db', None) is not None:
            try:
                gone = self.target_db.delete_many([int((r.get('id') or 0)) for r in rows])
            except Exception as e:
                self._recon_warn(f"清除失败：{e}")
                return None
        self._set_digit_keys(())
        self._set_char_keys(False)
        self.recon = {'state': 'saved', 'target': None, 'grid': '', 'name': ''}
        self._recon_mini_drop()
        self.recon_last = None
        self.view.target = None
        self.view.buddy = None
        self.view.update()
        self.refresh_targets_label()
        self.recon_recompute()
        self.lb_recon_report.setText(('已复制到剪贴板：\n' if ok else '复制失败，可手动复制：\n') + lines)
        self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
        self.lb_status.setText(f"已清除 {gone} 个目标" + (' · 消灭通报已复制' if ok else ' · 复制失败'))

    def _finish_mark(self, picked):
        """picked=(分类, 型号, hp, ticket, 车种key)；None = 只标记位置不入库。"""
        cur = self.recon or {}
        tgt = cur.get('target')
        grid_txt = (cur.get('grid') or (self.map.grid_at(*tgt, 4) if tgt else ''))
        self._set_digit_keys(())
        self._set_char_keys(False)
        if tgt is None:
            self.recon_clear('侦察流程缺目标坐标，已重置')
            return None
        self.view.target = tgt
        hk_rpt = self.hk_texts.get(14, 'F11')
        hk_mark = self.hk_texts.get(11, 'F5')
        if picked is None:
            self.view.target_title = '敌目标'
            self.view.update()
            self.recon = {'state': 'saved', 'target': tgt, 'grid': grid_txt, 'name': ''}
            self._recon_mini_drop()
            self.recon_recompute()
            self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
            self.lb_status.setText(f"只标记了位置：{grid_txt}（没入库）—— 按 {hk_mark} 标下一个")
            return None
        cat_label, name, hp, ticket, cat_key = picked
        h = self.map.h_at(*tgt)
        tid = None
        uid = ''
        if getattr(self, 'target_db', None) is not None:
            try:
                uid = (self.target_db.next_uid(self.map.slug) or '')
                tid = self.target_db.add(self.map.slug, grid_txt, tgt[0], tgt[1], h, (self.cfg.get('r_faction') or ''), cat_label, name, hp, ticket, getattr(self, 'recon_sub', 'scope'), uid)
            except Exception as e:
                print('写目标库失败:', e)
        clip = f"{uid} {name} {grid_txt}".strip()
        ok = set_clipboard_text(clip)
        self.view.target_title = f"{name} {uid}".strip()
        self.view.update()
        self.recon = {'state': 'saved', 'target': tgt, 'grid': grid_txt, 'name': name, 'tid': tid, 'cat': cat_label, 'uid': uid}
        self._recon_mini_drop()
        self.refresh_targets_label()
        self.recon_recompute()
        if ok:
            self.lb_recon_report.setText(("""已复制到剪贴板：
""" + clip))
        self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
        extra = '' if uid else '（本图 50 个 UID 已用完，这条没分配 UID —— 先清除已消灭的目标）'
        self.lb_status.setText(f"已记入目标库：{(uid or '无 UID')} · {grid_txt} · {name}（{hp} hp / {ticket} 票）{extra} —— 按 {hk_mark} 标下一个，或按 {hk_rpt} 出报告")

    def _finish_update(self, grid_txt):
        """「更新已有目标的位置」收尾：只改坐标，UID / 型号不变，并复制到剪贴板。"""
        cur = self.recon or {}
        tgt = cur.get('target')
        tid = cur.get('upd_tid')
        uid = (cur.get('upd_uid') or '')
        name = (cur.get('upd_name') or '敌对目标')
        self._set_digit_keys(())
        self._set_char_keys(False)
        if tgt is None or not tid:
            self.recon_clear('更新目标失败：缺坐标或记录，已重置')
            return None
        h = self.map.h_at(*tgt)
        ok_db = True
        if getattr(self, 'target_db', None) is not None:
            try:
                self.target_db.update_pos(int(tid), grid_txt, tgt[0], tgt[1], h, getattr(self, 'recon_sub', 'scope'))
            except Exception as e:
                ok_db = False
                print('更新目标位置失败:', e)
        clip = f"{uid} {name} {grid_txt}".strip()
        ok = set_clipboard_text(clip)
        self.view.target = tgt
        self.view.target_title = f"{name} {uid}".strip()
        self.view.update()
        self.recon = {'state': 'saved', 'target': tgt, 'grid': grid_txt, 'name': name, 'tid': int(tid), 'uid': uid}
        self.refresh_targets_label()
        self.recon_recompute()
        if ok:
            self.lb_recon_report.setText(("""已复制到剪贴板：
""" + clip))
        self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;' if ok_db else 'color:#ff8f8f;font-size:12px;')
        self.lb_status.setText(f"已更新目标位置：{clip}" if ok_db else (f"更新失败：{name}（{uid}）没写进目标库" + (' · 已复制' if ok else ' · 复制失败')))

    def recon_report(self):
        if self.mode != 'recon':
            self._recon_warn('当前不是侦察报告模式：先切到「侦察报告」再用报告键')
            return None
        if getattr(self, 'target_db', None) is None:
            self._recon_warn('目标库打不开（targets.db）')
            return None
        try:
            rows = self.target_db.list_map(self.map.slug)
        except Exception as e:
            self._recon_warn(f"读目标库失败：{e}")
            return None
        else:
            if not rows:
                self._recon_warn('这张图上还没标记过敌方目标：先按标记键截一个', '目标库是空的 · 先标记')
                return None
            self._set_digit_keys('0123456789')
            self._set_char_keys(False)
            self.overlay.clear_options()
            self.recon = {'state': 'rpt_pick', 'rows': rows, 'page': 0}
            self._recon_mini_drop()
            self.recon_recompute()
            self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
            self.lb_status.setText('浮窗里按数字选要报告的目标（9 下一页 · 0 取消）')

    def recon_buddy_char(self, ch):
        st = (self.recon or {}).get('state')
        if st not in ('buddy', 'rpt_buddy'):
            return None
        buf = (self.recon.get('buf') or '')
        n = len(buf)
        if n == 0:
            if not ch.isalpha():
                self._recon_warn('队友坐标第 1 位必须是字母（如 B、L）')
                return None
        elif n < 3:
            if not ch.isdigit():
                self._recon_warn('队友坐标第 2–3 位必须是数字（B7 要打成 B07）')
                return None
        elif (not ch.isdigit() or ch == '0'):
            self._recon_warn('队友坐标最后三位各是 1–9（子格位）')
            return None
        buf += ch
        self.recon['buf'] = buf
        if len(buf) < 6:
            shown = self._recon_buf_text(buf)
            self.recon_recompute()
            self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
            self.lb_status.setText(f"队友坐标 {shown} · 再输 {(6 - len(buf))} 位（共 6 位）")
            return None
        if st == 'rpt_buddy':
            self.recon_do_report()
            return None
        self.recon_calc()

    @staticmethod
    def _recon_buf_text(buf):
        if not buf:
            return ''
        if len(buf) < 3:
            return buf
        out = f"{buf[0]}{buf[1:3]}"
        for c in buf[3:]:
            out += f"-{c}"
        return out

    def _buddy_point(self, text):
        try:
            bx, bz = core.parse(text)
        except core.ParseError as e:
            self._recon_warn(str(e))
            return None
        else:
            if bx > (self.map.spanx + 1) or bz > (self.map.spanz + 1):
                self._recon_warn(f"队友坐标 {text} 不在这张地图上")
                return None
            return (min(bx, self.map.spanx), min(bz, self.map.spanz))

    def recon_calc(self):
        buf = (self.recon.get('buf') or '')
        if len(buf) != 6:
            return None
        text = self._recon_buf_text(buf)
        buddy = self._buddy_point(text)
        if buddy is None:
            return None
        tx, tz = self.recon['target']
        dx, dz = tx - buddy[0], tz - buddy[1]
        bdist = math.hypot(dx, dz)
        brg = ((math.degrees(math.atan2(dx, (-dz))) + 360.0) % 360.0)
        msg = f"队友{text}，有敌人在你的{brg:.0f}度方向，{bdist:.0f}米处"
        ok = set_clipboard_text(msg)
        self.recon['buddy_txt'] = text
        self.recon['buddy_pt'] = buddy
        self.recon['state'] = 'done'
        self._set_char_keys(False)
        self.recon_last = {'msg': msg, 'me': self.recon.get('pos_txt', ''), 'az': self.recon.get('az'), 'dist': self.recon.get('dist'), 'mate': text, 'brg': brg, 'bdist': bdist, 'tg': self.map.grid_at(tx, tz, 4), 'target': (tx, tz), 'buddy_pt': buddy, 'name': ''}
        self.view.buddy = buddy
        self.view.target = (tx, tz)
        self.view.update()
        self.recon_recompute()
        self.lb_recon_report.setText((("已复制到剪贴板：\n" + msg) if ok else ("复制失败，可手动复制：\n" + msg)))
        self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
        self.lb_status.setText((("侦察报告已生成并复制：" + msg) if ok else ("侦察报告已生成（复制失败）：" + msg)))

    def recon_do_report(self):
        cur = self.recon or {}
        row = (cur.get('row') or {})
        buf = (cur.get('buf') or '')
        if len(buf) != 6:
            return None
        text = self._recon_buf_text(buf)
        buddy = self._buddy_point(text)
        if buddy is None:
            return None
        tx, tz = row.get('rx'), row.get('rz')
        if tx is None or tz is None:
            self._recon_warn('这条目标记录没坐标，删掉重记一条')
            return None
        dx, dz = tx - buddy[0], tz - buddy[1]
        bdist = math.hypot(dx, dz)
        brg = ((math.degrees(math.atan2(dx, (-dz))) + 360.0) % 360.0)
        name = (row.get('name') or '敌方目标')
        uid = (row.get('uid') or '')
        who = f"{name} {uid}".strip()
        gtxt = (row.get('grid') or self.map.grid_at(tx, tz, 4))
        msg = f"队友{text}，有敌人（{who}）在你的{brg:.0f}度方向，{bdist:.0f}米处，坐标{gtxt}"
        ok = set_clipboard_text(msg)
        if getattr(self, 'target_db', None) is not None:
            try:
                self.target_db.mark_reported(int((row.get('id') or 0)))
            except Exception:
                pass
        self._set_char_keys(False)
        self.recon = {'state': 'done', 'target': (tx, tz), 'grid': gtxt, 'name': name, 'uid': uid}
        self.recon_last = {'msg': msg, 'me': '', 'az': None, 'dist': None, 'mate': text, 'brg': brg, 'bdist': bdist, 'tg': gtxt, 'target': (tx, tz), 'buddy_pt': buddy, 'name': name, 'uid': uid}
        self.view.buddy = buddy
        self.view.target = (tx, tz)
        self.view.target_title = name
        self.view.update()
        self.recon_recompute()
        self.lb_recon_report.setText((("已复制到剪贴板：\n" + msg) if ok else ("复制失败，可手动复制：\n" + msg)))
        self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
        self.lb_status.setText((("侦察报告已生成并复制：" + msg) if ok else ("侦察报告已生成（复制失败）：" + msg)))

    def recon_recompute(self):
        if self.mode != 'recon':
            return None
        hk_mark = self.hk_texts.get(11, 'F5')
        hk_rpt = self.hk_texts.get(14, 'F11')
        cur = self.recon or {}
        last = self.recon_last or {}
        st = cur.get('state')
        live = st not in (None, 'done', 'saved')
        sub = {'drone': '无人机视角', 'map': '地图视角'}.get(getattr(self, 'recon_sub', 'scope'), '望远镜视角')
        pos_txt = (cur.get('pos_txt') or '') if getattr(self, 'recon_sub', 'scope') == 'scope' else ''
        az = cur.get('az')
        dist = cur.get('dist')
        mate = ''
        if st in ('buddy', 'rpt_buddy'):
            mate = (cur.get('buf') or '')
            if mate and '-' not in mate:
                mate = self._recon_buf_text(mate)
        tg = ''
        if st == 'tgt_mini':
            tg = f"{(cur.get('pos_txt') or '')}（3 级 · 等点图修正）"
        elif st == 'raz' and cur.get('wait_az') == 'tgt3':
            tg = f"{(cur.get('pos_txt') or '')}（3 级 · 等方位角）"
        elif cur.get('target'):
            tg = (cur.get('grid') or self.map.grid_at(*cur['target'], 4))
        elif st == 'mcoord':
            b = (cur.get('buf') or '')
            if b:
                pass
            else:
                pass
            tg = ''
        elif cur.get('pend4'):
            tg = f"{(cur.get('pos_txt') or '')}-{cur['pend4']}"
        if cur.get('name'):
            tg = f"{tg} · {cur['name']}"
        if cur.get('uid'):
            tg = f"{tg} · {cur['uid']}"
        measured = ''
        if az is not None or dist is not None:
            a = f"{az:.0f}°" if az is not None else '—'
            d = f"{dist:.0f} m" if dist is not None else '—'
            measured = f"{a} · {d}"
        mateview = ''
        if not live and last:
            mateview = f"{last['brg']:.0f}° · {last['bdist']:.0f} m"
        vals = {'mode': f"侦察报告 · {sub}", 'me': (pos_txt or '—'), 'measured': (measured or '—'), 'mate': (mate or '—'), 'mateview': (mateview or '—'), 'tg': (tg or '—')}
        for k, v in vals.items():
            self.rvals[k].setText(v)
        if getattr(self, 'recon_sub', 'scope') in ('drone', 'map'):
            rows = [('模式', vals['mode']), ('目标', vals['tg'])]
            if vals['mateview'] != '—':
                rows.append(('队友看', vals['mateview']))
            if vals['mate'] != '—':
                rows.append(('队友', vals['mate']))
            self.overlay.set_rows(rows)
        else:
            self.overlay.set_rows([('模式', vals['mode']), ('我', vals['me']), ('我测得', vals['measured']), ('队友', vals['mate']), ('队友看', vals['mateview']), ('目标', vals['tg'])])
        if st in ('type', 'item', 'common'):
            self._show_pick_options(st)
            if cur.get('mini'):
                self.overlay.set_note('小地图上点目标的真实位置（点完 1 秒自动收起）')
                self.overlay.set_status('done', f"点小地图修正目标 · {(cur.get('grid') or '')}"[:40])
                return None
            self.overlay.set_note('')
            self.overlay.set_status('idle', f"侦察 · 选目标 {(cur.get('grid') or '')}"[:40])
            return None
        if st == 'tgt_mini':
            self.overlay.clear_options()
            self.overlay.set_note('小地图上点目标的真实位置（退格 = 取消，重新截坐标）')
            self.overlay.set_status('done', f"点小地图修正目标 · {(cur.get('pos_txt') or '')}"[:40])
            return None
        if st == 'upd_mini':
            self.overlay.clear_options()
            self.overlay.set_note('小地图上点目标的真实位置（退格 = 取消，记录不动）')
            self.overlay.set_status('done', f"点小地图修正目标 · {(cur.get('grid') or '')}"[:40])
            return None
        if st == 'rpt_pick':
            self._show_report_options((cur.get('rows') or []), int((cur.get('page') or 0)))
            self.overlay.set_note('')
            self.overlay.set_status('idle', '侦察 · 选要报告的目标')
            return None
        if st == 'mark_mode':
            self._show_mark_mode_options()
            self.overlay.set_note('')
            self.overlay.set_status('idle', '侦察 · 标记新目标 / 更新位置')
            return None
        if st == 'upd_pick':
            self._show_rows_options('要更新哪个目标的位置？', (cur.get('rows') or []), int((cur.get('page') or 0)), '9 下一页 · 0 取消')
            self.overlay.set_note('')
            self.overlay.set_status('idle', '侦察 · 选要更新的目标')
            return None
        if st == 'clr_choose':
            self._show_clear_choose_options((cur.get('rows') or []))
            self.overlay.set_note('')
            self.overlay.set_status('idle', '侦察 · 清除目标')
            return None
        if st == 'clr_pick':
            self._show_clear_pick_options(cur)
            self.overlay.set_note('')
            self.overlay.set_status('idle', '侦察 · 选要清除的目标')
            return None
        self.overlay.clear_options()
        if live:
            src_name = self._src_name(getattr(self, 'recon_src', 'cmd'))
            pnote = (f"方位 {az:.0f}° 已记 · 按 {hk_mark} 读你的坐标（2/3）" if az is not None else f"按 {hk_mark} 读无人机方位角（1/2）")
            rnote = (f"坐标已记 · 读{src_name}的方位角（2/2）" if cur.get('wait_az') else f"按 {hk_mark} 读无人机方位角（1/2）")
            notes = {'raz': rnote, 'pos': pnote, 'target': f"按 {hk_mark} 截敌目标坐标", 'digit': '补 1–9 · 输错在距离那步退格退回', 'digit4': f"补 1–9 后按 {hk_mark} 确认", 'digit4_ok': f"按 {hk_mark} 确认入库 · 退格重输", 'mcoord': f"格号 {len((cur.get('buf') or ''))}/6 · 退格改 · {hk_mark} 确认", 'dist': f"输距离 {len((cur.get('digits') or ''))}/3 · 退格删一位", 'buddy': f"输队友坐标 {len((cur.get('buf') or ''))}/6 · 退格删一位", 'rpt_buddy': f"输队友坐标 {len((cur.get('buf') or ''))}/6 · 退格删一位"}
            self.overlay.set_note(notes.get(st, ''))
            self.overlay.set_status('idle', f"侦察 · {notes.get(st, '')}"[:40])
            return None
        if last:
            self.overlay.set_status('done', f"侦察报告 · {last['mate']}")
            self.overlay.set_note(f"按 {hk_mark} 标下一个 · 按 {hk_rpt} 再报一次")
            self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
            self.lb_status.setText(f"侦察报告：{last['msg']}")
            return None
        self.lb_recon_report.setText('')
        self.overlay.set_status('idle', f"侦察报告 · {sub}")
        self.overlay.set_note(f"按 {hk_mark} 标记敌方目标 · 按 {hk_rpt} 报告")
        self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
        self.lb_status.setText(f"侦察报告（{sub}）：按 {hk_mark} 标记敌方目标，按 {hk_rpt} 出报告")

    def _show_pick_options(self, st):
        f = (self._faction_data() or {})
        grid_txt = ((self.recon or {}).get('grid') or '')
        if st == 'type':
            cats = (f.get('cats') or [])
            items = [(str((i + 1)), f"{c.get('label')}（{len((c.get('items') or []))}）") for i, c in enumerate(cats[:8])]
            items.append(('9', '通用目标（步兵 / FOB / 迫击炮…）'))
            items.append(('0', '只标记位置，不入库'))
            self.overlay.set_options(f"目标 {grid_txt} · 这是哪种敌方目标？", items, '按数字选分类 · 0 只标记不入库')
            return None
        if st == 'item':
            cat = (self._cat_of((self.recon or {}).get('cat')) or {})
            allit = (cat.get('items') or [])
            page = int(((self.recon or {}).get('page') or 0))
            start = page * 8
            chunk = allit[start:(start + 8)]
            items = [(str((i + 1)), (it.get('name') or '?')) for i, it in enumerate(chunk)]
            items.append(('0', '返回分类'))
            foot = '0 返回分类'
            if (start + 8) < len(allit):
                items.insert(8, ('9', f"下一页（{(page + 2)}/{((len(allit) + 7) // 8)}）"))
                foot = '9 下一页 · 0 返回分类'
            self.overlay.set_options(f"{cat.get('label', '')}（{grid_txt}）", items, foot)
            return None
        if st == 'common':
            commons = (((self.recon_targets or {}).get('common') or {}).get('items') or [])
            items = [(str((i + 1)), (it.get('label') or it.get('key'))) for i, it in enumerate(commons[:8])]
            items.append(('0', '返回分类'))
            self.overlay.set_options(f"通用目标（{grid_txt}）", items, '0 返回分类')
            return None
        return None

    def _show_report_options(self, rows, page=0):
        start = page * 8
        chunk = rows[start:(start + 8)]
        items = []
        for i, r in enumerate(chunk):
            nm = (r.get('name') or '目标')
            items.append((str((i + 1)), f"{(r.get('uid') or '无 UID')} {nm} · {(r.get('grid') or '?')}"))
        if (start + 8) < len(rows):
            items.append(('9', f"下一页（{(page + 2)}/{((len(rows) + 7) // 8)}）"))
        items.append(('0', '取消'))
        self.overlay.set_options(f"要报告哪个目标？（本图 {len(rows)} 条）", items, '按数字选目标 · 0 取消')

    def _show_mark_mode_options(self):
        n = int(((self.recon or {}).get('n') or 0))
        items = [('1', '标记新目标（分配新 UID）')]
        if n:
            items.append(('2', f"更新已有目标的位置（本图 {n} 条）"))
        items.append(('0', '取消'))
        if n:
            self.overlay.set_options('标记方式', items, '1 新目标 · 2 更新已有 · 0 取消')
            return None
        self.overlay.set_options('标记方式', items, '1 新目标 · 0 取消')

    def _show_rows_options(self, title, rows, page, foot):
        start = page * 8
        chunk = rows[start:(start + 8)]
        items = []
        for i, r in enumerate(chunk):
            uid = (r.get('uid') or '无 UID')
            nm = (r.get('name') or '目标')
            items.append((str((i + 1)), f"{uid} {nm} · {(r.get('grid') or '?')}"))
        if (start + 8) < len(rows):
            items.append(('9', f"下一页（{(page + 2)}/{((len(rows) + 7) // 8)}）"))
        items.append(('0', '取消'))
        self.overlay.set_options(f"{title}（本图 {len(rows)} 条）", items, foot)

    def _show_clear_choose_options(self, rows):
        items = [('1', '选择要清除的目标'), ('2', f"全部清除（{len(rows)} 个）"), ('0', '取消')]
        self.overlay.set_options('清除目标', items, '1 选目标 · 2 全清 · 0 取消')

    def _show_clear_pick_options(self, cur):
        rows = (cur.get('rows') or [])
        page = int((cur.get('page') or 0))
        sel = {int(x) for x in (cur.get('sel') or [])}
        start = page * 8
        chunk = rows[start:(start + 8)]
        items = []
        for i, r in enumerate(chunk):
            mark = '● ' if int((r.get('id') or 0)) in sel else ''
            uid = (r.get('uid') or '无 UID')
            nm = (r.get('name') or '目标')
            items.append((str((i + 1)), f"{mark}{uid} {nm} · {(r.get('grid') or '?')}"))
        if (start + 8) < len(rows):
            items.append(('9', f"下一页（{(page + 2)}/{((len(rows) + 7) // 8)}）"))
        items.append(('0', f"确认清除已选（{len(sel)} 个）"))
        self.overlay.set_options(f"选要清除的目标（本图 {len(rows)} 条）", items, '1–8 选中/取消 · 9 下一页 · 0 确认清除')

    CMD_TASKS = ('观察', '移动', '进攻', '防守', '建造', '打火力支援标', '放电台', '放队包')
    CMD_CATS = (('functional', '功能性建筑'), ('fortifications', '防御工事'), ('weapons', '武器'))
    CMD_ATT = ('立即执行', '完成手里的事情再执行')
    def _cmd_faction_obj(self):
        for f in (self.command_data.get('factions') or []):
            if not f.get('id') == self.cmd_faction:
                continue
            return f

    def _cmd_unit(self):
        f = self._cmd_faction_obj()
        if not f:
            return None
        for u in (f.get('units') or []):
            if not u.get('key') == self.cmd_unit:
                continue
            return u

    def _cmd_cat_label(self, key):
        for k, label in self.CMD_CATS:
            if not k == key:
                continue
            return label
        return key or '—'

    def _refresh_cmd_combos(self):
        if not hasattr(self, 'cb_cfaction'):
            return None
        self.cb_cfaction.blockSignals(True)
        self.cb_cfaction.clear()
        self.cb_cfaction.addItem('（先选我方阵营）', '')
        for f in (self.command_data.get('factions') or []):
            self.cb_cfaction.addItem(str((f.get('label') or f.get('id'))), str(f.get('id')))
        idx = self.cb_cfaction.findData(self.cmd_faction)
        if idx < 0:
            self.cmd_faction = ''
            idx = 0
        self.cb_cfaction.setCurrentIndex(idx)
        self.cb_cfaction.blockSignals(False)
        self._reload_cmd_units()

    def _reload_cmd_units(self):
        if not hasattr(self, 'cb_cunit'):
            return None
        self.cb_cunit.blockSignals(True)
        self.cb_cunit.clear()
        f = self._cmd_faction_obj()
        if not f:
            self.cb_cunit.addItem('（先选阵营）', '')
            self.cmd_unit = ''
        else:
            for u in (f.get('units') or []):
                self.cb_cunit.addItem(f"{u.get('name')}（{u.get('type_cn')}）", str(u.get('key')))
            idx = self.cb_cunit.findData(self.cmd_unit)
            if idx < 0:
                idx = 0
                if f.get('units'):
                    self.cmd_unit = str(f['units'][0].get('key'))
                    self.cfg['c_unit'] = self.cmd_unit
                    save_config(self.cfg)
            self.cb_cunit.setCurrentIndex(max(0, idx))
        self.cb_cunit.blockSignals(False)
        self._cmd_unit_label()

    def _cmd_unit_label(self):
        if not hasattr(self, 'lb_cunit'):
            return None
        u = self._cmd_unit()
        if not u:
            self.lb_cunit.setText('还没选我方部队：先选阵营，再选到具体部队')
            return None
        self.lb_cunit.setText(f"当前：{u.get('name')} · {u.get('type_cn')} — 功能建筑 {len((u.get('functional') or []))} · 防御工事 {len((u.get('fortifications') or []))} · 武器 {len((u.get('weapons') or []))}")

    def on_cmd_faction_changed(self, idx):
        fid = str((self.cb_cfaction.itemData(idx) or ''))
        self.cmd_faction = fid
        self.cfg['c_faction'] = fid
        f = self._cmd_faction_obj()
        if f and f.get('units'):
            pass
        else:
            pass
        self.cmd_unit = ''
        self.cfg['c_unit'] = self.cmd_unit
        save_config(self.cfg)
        self._refresh_cmd_combos()
        if fid:
            self.cmd_reset('已选我方阵营，重新开始输入')
            return None
        self.cmd_reset('还没选我方阵营')

    def on_cmd_unit_changed(self, idx):
        self.cmd_unit = str((self.cb_cunit.itemData(idx) or ''))
        self.cfg['c_unit'] = self.cmd_unit
        save_config(self.cfg)
        self._cmd_unit_label()
        self.cmd_reset('已切换我方部队，重新开始输入')

    def _cmd_tip(self):
        hk = self.hk_texts.get(31, 'Alt+F12')
        clear = self.hk_texts.get(32, 'Alt+Shift+F12')
        return f"发布命令玩法：\n① 先选「我方阵营 + 部队」：建造选项来自这套编制表（兵站/弹药箱/维修站 + 工事 + 武器）\n② 按 {hk} 开始：小键盘输 1–2 位队长编号，再按 {hk} 确认\n③ 输 4 级坐标：字母键选列 + 小键盘 2 位行号 + 3 位子格（G7 要打成 G07），按 {hk} 确认\n④ 数字键选任务（建造再选 功能性建筑 / 防御工事 / 武器），按 {hk} 确认\n⑤ 数字键选态度，按 {hk} 确认 → 整句命令复制到剪贴板并显示在浮窗\nBACKSPACE 删一位 / 退回上一步；{clear} 清空重来"

    def _cmd_hint(self):
        hk = self.hk_texts.get(31, 'Alt+F12')
        st = (self.cmd or {}).get('state')
        if st is None:
            return f"按 {hk} 开始：① 队长 → ② 坐标 → ③ 任务 → ④ 态度；每步输完再按一次确认（小键盘输数字）"
        if st == 'leader':
            n = len(((self.cmd or {}).get('leader') or ''))
            return f"① 队长：用小键盘输 1–2 位（已输 {n}/2），BACKSPACE 删一位，按 {hk} 确认"
        if st == 'grid':
            n = len(((self.cmd or {}).get('grid') or ''))
            return f"② 坐标：字母键选列 + 小键盘输行号与子格（G7 打成 G07；已输 {n}/6），BACKSPACE 删一位，按 {hk} 确认"
        if st == 'task':
            return f"③ 任务：小键盘 1–8 选择，BACKSPACE 退回坐标，按 {hk} 确认"
        if st == 'build_cat':
            return f"③ 建造：1 功能性建筑 / 2 防御工事 / 3 武器，BACKSPACE 退回任务，按 {hk} 确认"
        if st == 'build_item':
            cat = self._cmd_cat_label(((self.cmd or {}).get('build_cat') or ''))
            return f"③ 建造 · {cat}：数字键 1–8 选，9 下一页 / 0 返回，BACKSPACE 退回类别，按 {hk} 确认"
        if st == 'attitude':
            return f"④ 态度：1 立即执行 / 2 完成手里的事情再执行，BACKSPACE 退回，按 {hk} 确认"
        return f"按 {hk} 开始发布命令"

    def _cmd_progress(self):
        c = self.cmd or {}
        st = c.get('state')
        if st == 'leader':
            if c.get('leader'):
                return ((c.get('leader') or '—') + '队长')
            return ((c.get('leader') or '—') + '')
        if st == 'grid':
            return (self._recon_buf_text((c.get('grid') or '')) or '—')
        if st == 'task':
            return (c.get('task') or '—')
        if st == 'build_cat':
            if c.get('build_cat'):
                return ('建造 · ' + self._cmd_cat_label((c.get('build_cat') or '')))
            return '建造 · ' + '选类别'
        if st == 'build_item':
            it = (c.get('build_item') or {})
            return ('建造 · ' + (it.get('cn') or '选对象'))
        if st == 'attitude':
            return (c.get('attitude') or '—')
        return '—'

    def _cmd_options(self):
        c = self.cmd or {}
        st = c.get('state')
        if st == 'task':
            items = [(str((i + 1)), t) for i, t in enumerate(self.CMD_TASKS)]
            return ('③ 干什么事情', items, '1–8 选择 · BACKSPACE 退回 · 确认键确认')
        if st == 'build_cat':
            items = [(str((i + 1)), label) for i, (_k, label) in enumerate(self.CMD_CATS)]
            return ('③ 建造 · 选类别', items, '1 功能建筑 · 2 防御工事 · 3 武器')
        if st == 'build_item':
            u = (self._cmd_unit() or {})
            rows = (u.get((c.get('build_cat') or '')) or [])
            page = int((c.get('page') or 0))
            chunk = rows[(page * 8):((page * 8) + 8)]
            items = []
            for i, it in enumerate(chunk):
                cost = it.get('cost')
                limit = it.get('limit')
                tail = []
                if cost is not None:
                    tail.append(str(cost))
                if limit is not None and int(limit) >= 0:
                    tail.append(f"限{limit}")
                label = (it.get('cn') or it.get('name') or it.get('key'))
                if tail:
                    items.append((str((i + 1)), f"{label}（{' · '.join(tail)}）"))
                else:
                    items.append((str((i + 1)), label))
            foot = '0 返回类别'
            if ((page * 8) + 8) < len(rows):
                foot = (f"9 下一页（{(page + 2)}/{((len(rows) + 7) // 8)}） · " + foot)
            return (('③ 建造 · ' + self._cmd_cat_label((c.get('build_cat') or ''))), items, foot)
        if st == 'attitude':
            items = [(str((i + 1)), t) for i, t in enumerate(self.CMD_ATT)]
            return ('④ 态度', items, '1 立即执行 · 2 完成手里的事情再执行')
        return ('', [], '')

    def _cmd_title(self):
        titles = {'leader': '① 队长编号', 'grid': '② 去哪里行动（4 级坐标）', 'task': '③ 干什么事情', 'build_cat': '③ 建造 · 选类别', 'build_item': '③ 建造 · 选对象', 'attitude': '④ 态度'}
        return titles.get((self.cmd or {}).get('state'), '待开始')

    def _cmd_rows(self):
        u = self._cmd_unit()
        who = f"{u.get('name')}（{u.get('type_cn')}）" if u else '未选我方部队'
        return [('我方', who), ('进度', self._cmd_title()), ('已输入', self._cmd_progress())]

    def _cmd_overlay_idle(self):
        """命令模式空闲时的浮窗内容：上一条整句，或「我方 / 进度 / 已输入」三行。"""
        if not hasattr(self, 'overlay'):
            return None
        hk = self.hk_texts.get(31, 'Alt+F12')
        self.overlay.clear_options()
        if self.cmd_last:
            self.overlay.set_para('上一条命令', self.cmd_last, f"按 {hk} 发布下一条", 'idle', '命令 · 上一条')
            return None
        have = bool(self._cmd_unit())
        self.overlay.set_rows(self._cmd_rows(), f"按 {hk} 开始：选队长 → 坐标 → 任务 → 态度" if have else '先在面板里选我方阵营和部队')
        self.overlay.set_status('idle', '命令 · 待开始' if have else '命令 · 先选我方部队')

    def _sync_mode_overlay(self):
        """浮窗只显示当前大模式的内容：间瞄=诸元四行，侦察=侦察行/选项，发布命令=命令行/整句。"""
        if not hasattr(self, 'overlay'):
            return None
        if self.mode == 'recon':
            self.recon_recompute()
            return None
        if self.mode == 'barrage':
            self.bar_recompute()
            return None
        if self.mode == 'command':
            if self.cmd:
                self._cmd_show()
                return None
            self._cmd_overlay_idle()
            return None
        self.recompute()

    def _cmd_show(self, error='', show=True):
        if not hasattr(self, 'lb_cmd_step'):
            return None
        title = self._cmd_title()
        self.lb_cmd_step.setStyleSheet('color:#ff8f8f;font-size:12px;' if error else 'color:#ffd166;font-size:12px;')
        self.lb_cmd_step.setText((error or self._cmd_hint()))
        if not show:
            return None
        self.overlay.set_rows(self._cmd_rows())
        t, items, foot = self._cmd_options()
        if items:
            self.overlay.set_options(t, items, foot)
        else:
            self.overlay.clear_options()
        self.overlay.set_status('error' if error else 'idle', error or f"发布命令 · {title}")
        if not self.overlay.isVisible():
            self.overlay.show()
        self.overlay.raise_()

    def _cmd_sync_keys(self):
        st = (self.cmd or {}).get('state')
        if st == 'grid':
            self._set_digit_keys('0123456789')
            self._set_char_keys(True)
        elif st:
            self._set_digit_keys('0123456789')
            self._set_char_keys(False)
        else:
            self._set_digit_keys(())
            self._set_char_keys(False)
        self._set_mapnum_keys(False)

    def _cmd_warn(self, msg, short=''):
        self.lb_cmd_step.setStyleSheet('color:#ff8f8f;font-size:12px;')
        self.lb_cmd_step.setText(msg)
        self.overlay.set_rows(self._cmd_rows())
        self.overlay.set_status('error', (short or msg))
        if not self.overlay.isVisible():
            self.overlay.show()
        self.overlay.raise_()

    def cmd_step(self):
        """发布命令键：没开始时开始；已经开始就确认当前这一步。"""
        if getattr(self, 'mode', 'arty') != 'command':
            self._mode_hint('command')
            return None
        if not self._cmd_unit():
            self._cmd_warn('先在面板里选我方阵营和部队（建造选项来自这套编制表）', '先选我方阵营 / 部队')
            return None
        if self.cmd is None:
            self.cmd = {'state': 'leader', 'leader': '', 'grid': '', 'task': '', 'build_cat': '', 'build_item': None, 'attitude': '', 'page': 0}
            self._cmd_sync_keys()
            self._cmd_show()
            self.lb_cmd_step.setStyleSheet('color:#ffd166;font-size:12px;')
            self.lb_cmd_step.setText(self._cmd_hint())
            return None
        st = self.cmd.get('state')
        if st == 'leader':
            val = (self.cmd.get('leader') or '').strip()
            if not val:
                self._cmd_warn('还没输队长编号：用小键盘输 1–2 位，再按确认键', '先输队长编号')
                return None
            self.cmd['leader'] = str(int(val))
            self.cmd['state'] = 'grid'
            self._cmd_sync_keys()
            self._cmd_show()
            return None
        if st == 'grid':
            buf = (self.cmd.get('grid') or '')
            if len(buf) < 6:
                self._cmd_warn(f"坐标还差 {(6 - len(buf))} 位（共 6 位：字母 + 2 位行号 + 3 位子格）", f"坐标还差 {(6 - len(buf))} 位")
                return None
            txt = self._recon_buf_text(buf)
            try:
                gx, gz = core.parse(txt)
            except core.ParseError as e:
                self._cmd_warn(f"{txt} 不行：{e}", '坐标格式不对')
                return None
            if gx > self.map.spanx + 1 or gz > self.map.spanz + 1:
                self._cmd_warn(f"{txt} 不在这张地图上", '坐标不在这张图上')
                return None
            self.cmd['grid'] = txt
            self.view.target = (min(gx, self.map.spanx), min(gz, self.map.spanz))
            self.view.target_title = '行动地点'
            self.view.update()
            self.cmd['state'] = 'task'
            self._cmd_sync_keys()
            self._cmd_show()
            return None
        if st == 'task':
            if not self.cmd.get('task'):
                self._cmd_warn('还没选任务：小键盘 1–8 选一个，再按确认键', '先选任务')
                return None
            if self.cmd['task'] == '建造':
                self.cmd['state'] = 'build_cat'
                self.cmd['build_cat'] = ''
                self.cmd['build_item'] = None
            else:
                self.cmd['state'] = 'attitude'
            self._cmd_sync_keys()
            self._cmd_show()
            return None
        if st == 'build_cat':
            if not self.cmd.get('build_cat'):
                self._cmd_warn('还没选建造类别：1 功能性建筑 / 2 防御工事 / 3 武器', '先选建造类别')
                return None
            self.cmd['state'] = 'build_item'
            self.cmd['build_item'] = None
            self.cmd['page'] = 0
            self._cmd_sync_keys()
            self._cmd_show()
            return None
        if st == 'build_item':
            if not self.cmd.get('build_item'):
                self._cmd_warn('还没选建造对象：数字键选一个，再按确认键', '先选建造对象')
                return None
            self.cmd['state'] = 'attitude'
            self._cmd_sync_keys()
            self._cmd_show()
            return None
        if st == 'attitude':
            if not self.cmd.get('attitude'):
                self._cmd_warn('还没选态度：1 立即执行 / 2 完成手里的事情再执行', '先选态度')
                return None
            self._cmd_finish()
        return None

    def cmd_digit(self, d):
        if not self.cmd:
            return None
        st = self.cmd.get('state')
        if st == 'leader':
            if d == '0' and not self.cmd.get('leader'):
                self._cmd_warn('队长编号从 1 开始：输 1–99，最多 2 位', '队长编号从 1 开始')
                return None
            if len((self.cmd.get('leader') or '')) >= 2:
                self._cmd_warn('队长编号最多 2 位；按 BACKSPACE 删一位', '队长编号最多 2 位')
                return None
            self.cmd['leader'] = ((self.cmd.get('leader') or '') + d)
            self._cmd_show()
            return None
        if st == 'grid':
            buf = (self.cmd.get('grid') or '')
            if not buf:
                self._cmd_warn('先按字母键选列（如 G），再用小键盘输行号和子格', '先按字母键选列')
                return None
            if len(buf) >= 6:
                self._cmd_warn('已经输满 6 位：按确认键，或 BACKSPACE 删一位', '已输满 6 位')
                return None
            if len(buf) >= 3 and d == '0':
                self._cmd_warn('子格只能是 1–9（不能是 0）', '子格只能是 1–9')
                return None
            self.cmd['grid'] = (buf + d)
            self._cmd_show()
            return None
        if st == 'task':
            n = int(d)
            if 0 < n <= len(self.CMD_TASKS):
                self.cmd['task'] = self.CMD_TASKS[(n - 1)]
                self._cmd_show()
                return None
            self._cmd_warn(f"任务只有 1–{len(self.CMD_TASKS)} 号：看清浮窗里的编号再按", '任务编号不对')
            return None
        if st == 'build_cat':
            n = int(d)
            if 0 < n <= len(self.CMD_CATS):
                self.cmd['build_cat'] = self.CMD_CATS[(n - 1)][0]
                self.cmd['build_item'] = None
                self.cmd['page'] = 0
                self._cmd_show()
                return None
            self._cmd_warn('建造类别只有 1–3：1 功能性建筑 / 2 防御工事 / 3 武器', '建造类别只有 1–3')
            return None
        if st == 'build_item':
            u = (self._cmd_unit() or {})
            rows = (u.get((self.cmd.get('build_cat') or '')) or [])
            page = int((self.cmd.get('page') or 0))
            if d == '0':
                self.cmd['build_item'] = None
                self.cmd['state'] = 'build_cat'
                self._cmd_sync_keys()
                self._cmd_show()
                return None
            if d == '9':
                if ((page * 8) + 8) < len(rows):
                    self.cmd['page'] = (page + 1)
                    self.cmd['build_item'] = None
                    self._cmd_show()
                    return None
                self._cmd_warn('已经是最后一页了：0 返回类别，BACKSPACE 退回类别', '已经是最后一页')
                return None
            n = int(d)
            idx = (((page * 8) + n) - 1)
            if 1 <= n <= 8 and 0 <= idx < len(rows):
                self.cmd['build_item'] = rows[idx]
                self._cmd_show()
                return None
            left = max(0, (len(rows) - (page * 8)))
            self._cmd_warn(f"这一页只有 {left} 项：按 1–{min(8, left)} 选，0 返回类别", '编号超出这一页')
            return None
        if st == 'attitude':
            n = int(d)
            if 0 < n <= len(self.CMD_ATT):
                self.cmd['attitude'] = self.CMD_ATT[(n - 1)]
                self._cmd_show()
                return None
            self._cmd_warn('态度只有 1–2：1 立即执行 / 2 完成手里的事情再执行', '态度只有 1–2')
            return None

    def cmd_char(self, ch):
        if (not self.cmd or self.cmd.get('state') != 'grid'):
            return None
        ch = (ch or '').upper()
        if not ch:
            return None
        buf = (self.cmd.get('grid') or '')
        if len(buf) >= 6:
            self._cmd_warn('已经输满 6 位：按确认键，或 BACKSPACE 删一位', '已输满 6 位')
            return None
        i = len(buf)
        if i == 0:
            if not ch.isalpha():
                self._cmd_warn('第 1 位是列字母（A–Z），不是数字', '第 1 位要按字母键')
                return None
            buf = ch
        elif i < 3:
            if not ch.isdigit():
                self._cmd_warn('第 2–3 位是行号，用小键盘输数字（G7 打成 G07）', '行号要输数字')
                return None
            buf += ch
        else:
            if ch not in '123456789':
                self._cmd_warn('后面 3 位子格只能是 1–9', '子格只能是 1–9')
                return None
            buf += ch
        self.cmd['grid'] = buf
        self._cmd_show()

    def cmd_backspace(self):
        if not self.cmd:
            self._backspace_hint()
            return None
        st = self.cmd.get('state')
        if st == 'leader':
            self.cmd['leader'] = (self.cmd.get('leader') or '')[:-1]
        elif st == 'grid':
            self.cmd['grid'] = (self.cmd.get('grid') or '')[:-1]
        elif st == 'task':
            self.cmd['task'] = ''
            self.cmd['state'] = 'grid'
            self._cmd_sync_keys()
        elif st == 'build_cat':
            self.cmd['build_cat'] = ''
            self.cmd['build_item'] = None
            self.cmd['state'] = 'task'
            self._cmd_sync_keys()
        elif st == 'build_item':
            self.cmd['build_item'] = None
            self.cmd['page'] = 0
            self.cmd['state'] = 'build_cat'
            self._cmd_sync_keys()
        elif st == 'attitude':
            self.cmd['attitude'] = ''
            if self.cmd.get('task') == '建造':
                pass
            else:
                pass
            self.cmd['state'] = 'task'
            self._cmd_sync_keys()
        self._cmd_show()

    def cmd_reset(self, msg=''):
        self.cmd = None
        self._cmd_sync_keys()
        self.view.target = None
        self.view.update()
        if not hasattr(self, 'lb_cmd_step'):
            return None
        self.lb_cmd_step.setStyleSheet('color:#ffd166;font-size:12px;')
        self.lb_cmd_step.setText((msg or self._cmd_hint()))
        if msg:
            u = self._cmd_unit()
            who = f"{u.get('name')}（{u.get('type_cn')}）" if u else '未选我方部队'
            self.overlay.set_rows([('我方', who), ('进度', '—'), ('已输入', '—')], msg)
            self.overlay.clear_options()
            self.overlay.set_status('idle', msg)
            if not self.overlay.isVisible():
                self.overlay.show()
                return None
            return None

    def _cmd_sentence(self):
        c = self.cmd or {}
        leader = str(int((c.get('leader') or 1)))
        grid = (c.get('grid') or '')
        task = (c.get('task') or '')
        if task == '建造':
            it = (c.get('build_item') or {})
            task = ('建造' + str((it.get('short') or it.get('cn') or '')))
        att = (c.get('attitude') or '')
        return f"{leader}队长，你去{grid}执行{task}任务，{att}"

    def _cmd_finish(self):
        sentence = self._cmd_sentence()
        ok = set_clipboard_text(sentence, 8)
        self.cmd_last = sentence
        self.cmd_copy_ok = ok
        self.cmd = None
        self._cmd_sync_keys()
        self.lb_cmd_text.setText(sentence)
        hk = self.hk_texts.get(31, 'Alt+F12')
        clear = self.hk_texts.get(32, 'Alt+Shift+F12')
        self.lb_cmd_step.setStyleSheet('color:#9fe6f5;font-size:12px;')
        self.lb_cmd_step.setText(f"已复制到剪贴板 · 切回游戏 Ctrl+V 粘贴（再按 {hk} 发下一条）" if ok else f"剪贴板复制失败：在下面文本框选中整句复制（再按 {hk} 发下一条）")
        self.overlay.set_para('命令', sentence, f"再按 {hk} 发下一条 · {clear} 清空", 'done' if ok else 'error', '命令已复制' if ok else '剪贴板复制失败')
        self.overlay.show()
        self.overlay.raise_()
        print(f"[cmd] {sentence}", flush=True)

    def open_drill(self):
        samples = self._sample_coords()
        text = random.choice(samples)
        if self.mode == 'recon':
            grid_region = (self.cfg.get('r_region') or self.cfg.get('region'))
            az_region = self.cfg.get('r_region_az')
        elif self.arty_sub == 'scope':
            grid_region = (self.cfg.get('s_region') or self.cfg.get('region'))
            az_region = self.cfg.get('s_region_az')
        else:
            grid_region = self.cfg.get('region')
            az_region = None
        if not grid_region:
            self.lb_status.setStyleSheet('color:#ff8f8f;font-size:12px;')
            self.lb_status.setText('先框一下坐标区域再演练')
            return None
        dpr = (QGuiApplication.screenAt(QPoint(grid_region[0], grid_region[1])) or QGuiApplication.primaryScreen()).devicePixelRatio()
        self.drill = DrillWindow(grid_region, text, dpr)
        self.drill.show()
        self.drill.raise_()
        az_text = ''
        if az_region:
            az_text = ('%03d' % random.randrange(0, 360))
            self.drill_az = DrillWindow(az_region, az_text, dpr, kind='az')
            self.drill_az.show()
            self.drill_az.raise_()
        if self.mode == 'recon':
            self.lb_status.setText((((f"演练：坐标区域显示 {text}" + (f"、侦察方位角区域显示 {az_text}" if az_text else ''))) + f"，按 {self.hk_texts.get(11, 'F5')} 走侦察流程试试"))
            return None
        if self.arty_sub == 'scope':
            self.lb_status.setText((((f"演练：玩家坐标区域显示 {text}" + (f"、方位角区域显示 {az_text}" if az_text else ''))) + f"，按 {self.hk_texts.get(21, 'F8')} 走望远镜流程试试"))
            return None
        self.lb_status.setText(f"演练：坐标区域显示 {text}，按 {self.hk_text} 快速定位 / {self.hk_texts[2]} 准确定位试试")

    def _sample_coords(self):
        cand = []
        step = 600
        rx = 200
        while rx < self.map.spanx:
            rz = 200
            while rz < self.map.spanz:
                g = self.map.grid_at(rx, rz)
                if 'I' not in g and 'O' not in g:
                    cand.append(g)
                rz += step
            rx += step
        return cand or ['D4-5-7']

    def save_key(self):
        self.cfg['api_key'] = self.ed_key.text().strip()
        save_config(self.cfg)
        self.lb_key.setText('已保存到 config.json')

    def _sub_text(self):
        if getattr(self, 'mode', 'arty') == 'barrage':
            return f"{self.hk_texts.get(41, 'Ctrl+F1')} 弹幕流程 · {self.hk_texts.get(42, 'Ctrl+Shift+F1')} 清除 · {self.hk_texts.get(43, 'Ctrl+Alt+F1')} 浮窗"
        if getattr(self, 'mode', 'arty') == 'command':
            return f"{self.hk_texts.get(31, 'Alt+F12')} 发布/确认 · {self.hk_texts.get(32, 'Alt+Shift+F12')} 取消 · {self.hk_texts.get(33, 'Ctrl+F12')} 浮窗"
        if getattr(self, 'mode', 'arty') == 'recon':
            return f"{self.hk_texts.get(11, 'F5')} 标记 · {self.hk_texts.get(14, 'F11')} 报告 · {self.hk_texts.get(12, 'F6')} 清除 · {self.hk_texts.get(13, 'F7')} 浮窗"
        if getattr(self, 'arty_sub', 'drone') == 'scope':
            return f"望远镜：{self.hk_texts.get(21, 'F8')} 流程 · {self.hk_texts.get(22, 'F9')} 清除 · {self.hk_texts.get(23, 'F10')} 浮窗"
        return f"{self.hk_text} 快速 · {self.hk_texts[2]} 准确 · {self.hk_texts[3]} 清除 · {self.hk_texts[4]} 浮窗"

    def _hk_hint(self):
        tail = '（点输入框按下新键即改；同一套里不能重复，不同模式可共用同一个键）'
        if getattr(self, 'mode', 'arty') == 'barrage':
            return (f"弹幕键：{self.hk_texts.get(41, 'Ctrl+F1')} 流程/确认 · {self.hk_texts.get(42, 'Ctrl+Shift+F1')} 清除 · {self.hk_texts.get(43, 'Ctrl+Alt+F1')} 浮窗" + tail)
        if getattr(self, 'mode', 'arty') == 'command':
            return (f"命令键：{self.hk_texts.get(31, 'Alt+F12')} 发布/确认 · {self.hk_texts.get(32, 'Alt+Shift+F12')} 取消已输入 · {self.hk_texts.get(33, 'Ctrl+F12')} 浮窗" + tail)
        if getattr(self, 'mode', 'arty') == 'recon':
            return (f"侦察键：{self.hk_texts.get(11, 'F5')} 标记/下一步 · {self.hk_texts.get(14, 'F11')} 报告 · {self.hk_texts.get(12, 'F6')} 清除 · {self.hk_texts.get(13, 'F7')} 浮窗" + tail)
        if getattr(self, 'arty_sub', 'drone') == 'scope':
            return (f"望远镜键：{self.hk_texts.get(21, 'F8')} 流程 · {self.hk_texts.get(22, 'F9')} 清除 · {self.hk_texts.get(23, 'F10')} 浮窗" + tail)
        return f"当前：{self.hk_text} 快速 · {self.hk_texts[2]} 准确（3 次）· {self.hk_texts[3]} 清除 · {self.hk_texts[4]} 浮窗" + tail

    def _tip_text(self):
        if getattr(self, 'mode', 'arty') == 'barrage':
            f41 = self.hk_texts.get(41, 'Ctrl+F1')
            return f"呼叫弹幕玩法（几门迫击炮按起始线逐轮往前推）：\n① 面板「阵地库」里新建阵地（坐标 + 武器，最多 15 个），勾选这次参加的门数\n② 选起始点怎么标：无人机视角（读屏幕坐标）/ 望远镜视角（方位角 + 你的坐标 + 距离）\n③ 按 {f41}：读起始点 → 小地图上点一下定宽度（关于起始点对称，上限 10 米 × 参加门数，下限 10 米）\n④ 同一张小地图上再点一侧：定往哪边炸，点的深度就是推进距离（矩形长度）\n⑤ 输推进速度（米/轮，最小 10）：每一轮的目标线往前挪这么多\n⑥ 每轮每门的方位/密位都列在面板里，{f41} 翻下一轮；⌫ 退回上一步"
        if getattr(self, 'mode', 'arty') == 'command':
            f31 = self.hk_texts.get(31, 'Alt+F12')
            f32 = self.hk_texts.get(32, 'Alt+Shift+F12')
            return f"发布命令玩法：\n① 先在面板选「我方阵营」和具体部队（决定建造选项）\n② 切回游戏按 {f31}：小键盘输队长编号（1–99）→ 再按一次确认\n③ 输 4 级坐标 6 位：字母 + 2 位行号（不足补 0，G7 打 G07）+ 3 位子格 → 确认\n④ 选任务：观察 / 移动 / 进攻 / 防守 / 建造 / 打火力支援标 / 放电台 / 放队包；选建造再选类别与具体建筑 → 确认\n⑤ 选态度：立即执行 / 完成手里的事情再执行 → 确认\n⑥ 自动生成「N队长，你去坐标执行任务，态度」并复制到剪贴板，同时显示在浮窗\n⑦ 输错按 BACKSPACE 删一位/退回上一步；{f32} 取消全部已输入"
        if getattr(self, 'mode', 'arty') == 'recon':
            f5 = self.hk_texts.get(11, 'F5')
            fr = self.hk_texts.get(14, 'F11')
            if getattr(self, 'recon_sub', 'scope') == 'drone':
                return f"侦察报告 · 无人机视角玩法：\n① 先框「敌目标坐标区域」：无人机视角下的 CURRENT GRID（只框坐标文字）\n② 面板里选好「敌方阵营」（目标选项来自这套编制表）\n③ 切回游戏按 {f5}：先选「1 新目标 / 2 更新已有目标的位置」\n④ 把准星对准敌方目标：读出 3 级坐标 → 小键盘补最后一位 → 再按一下标记键确认\n⑤ 浮窗按数字选分类 → 型号：记进目标库并分配 UID（T01–T50），「UID 型号 坐标」自动复制到剪贴板\n⑥ 需要报告时按 {fr}：选一个已记目标 → 连打队友坐标（B 1 1 2 5 6）\n⑦ 清除键可单选/多选/全部清除，清除前自动复制「型号 UID 已经被消灭；」"
            if getattr(self, 'recon_sub', 'scope') == 'map':
                return f"侦察报告 · 地图视角玩法：\n① 不用框区域：这个子模式不截图，直接键格号\n② 面板里选好「敌方阵营」（目标选项来自这套编制表）\n③ 切回游戏按 {f5}：先选「1 新目标 / 2 更新已有目标的位置」\n④ 直接键目标的 4 级格号（字母 + 行号 + 三位子格，如 K 1 2 5 8 8），打错按退格删一位\n⑤ 输满 6 位后再按一次 {f5} 确认 → 浮窗选分类 → 型号：记进目标库并分配 UID（T01–T50），「UID 型号 坐标」自动复制到剪贴板\n⑥ 需要报告时按 {fr}：选一个已记目标 → 连打队友坐标（B 1 1 2 5 6）\n⑦ 清除键可单选/多选/全部清除，清除前自动复制「型号 UID 已经被消灭；」"
            return f"侦察报告 · 望远镜视角玩法：\n① 框好两块区域：你视角的方位角数字、CURRENT GRID /「玩家位置：」\n② 面板里选好「敌方阵营」\n③ 切回游戏按 {f5}：先选「1 新目标 / 2 更新已有目标的位置」\n④ 再读方位角、读你的 4 级坐标、输 3 位距离\n⑤ 浮窗选分类 → 选型号：记入目标库并分配 UID，「UID 型号 坐标」自动复制\n⑥ 报告键：选已记目标 → 连打队友 4 级坐标（B 1 1 2 5 6）\n⑦ 清除键可单选/多选/全部，清除前自动复制消灭通报"
        if getattr(self, 'arty_sub', 'drone') == 'scope':
            hk = self.hk_texts.get(21, 'F8')
            return f"望远镜视角玩法：\n① 框好两块区域：① 玩家坐标（CURRENT GRID）② 方位角数字（0–359）\n② 切回游戏，按 {hk}：先读你视角的方位角，再按一次读你自己的坐标\n③ 小键盘补第 4 段、再输 3 位距离（155 / 090 / 001），自动算出敌目标\n④ 敌目标用 4 级坐标（B11-2-5-6）；地图左上「查坐标」可直接定位\n⑤ 无人机视角那套键位与截图框在「无人机视角」子模式下，互不干扰"
        return f"玩法：① 在游戏里站稳，记下自己的格子（也可在这张地图上点出来）\n② 把准星附近那一小块 CURRENT GRID 框成识别区域\n③ 切回游戏：{self.hk_text} 快速定位（截 1 次直接出结果）；{self.hk_texts[2]} 准确定位（截 3 次取外心）；{self.hk_texts[3]} 清掉截错/没凑齐的坐标（上次算好的目标保留）\n④ 算完角度/方位会自动复制到剪贴板（迫击炮「密位 1326.4 方位 122.0°」、有射角的武器「射角 12.3° 方位 122.0°」），游戏聊天里直接粘贴\n⑤ 地图左上「查坐标」可输 4 级坐标定位；要读方位角请切「望远镜视角」子模式"

    def refresh_hotkey_texts(self):
        if not hasattr(self, 'btn_manual'):
            return None
        self.lb_sub.setText(self._sub_text())
        if hasattr(self, 'lb_hk'):
            self.lb_hk.setText(self._hk_hint())
        if hasattr(self, 'lb_scope'):
            self.lb_scope.setText(self._scope_hint())
        if hasattr(self, 'btn_sub_drone'):
            self.btn_sub_drone.setText('无人机视角')
        self.btn_manual.setText(f"快速定位（{self.hk_text}）")
        self.btn_exact.setText(f"准确定位（{self.hk_texts[2]}）")
        self.btn_reset_pending.setToolTip(f"等同按清除坐标键（{self.hk_texts[3]}）：清掉还没算成目标的坐标，上次算好的目标保留")
        self.lb_tip.setText(self._tip_text())
        if hasattr(self, 'btn_recon_step'):
            self.btn_recon_step.setText(f"标记 / 下一步（{self.hk_texts.get(11, 'F5')}）")
            self.btn_recon_report.setText(f"报告（{self.hk_texts.get(14, 'F11')}）")
            self.btn_recon_clear.setText(f"清除侦察目标（{self.hk_texts.get(12, 'F6')}，可单选/多选/全部）")
            self.lb_recon.setText(self._recon_hint())
        if hasattr(self, 'btn_cmd_run'):
            self.btn_cmd_run.setText('发布命令 / 确认')
            self.btn_cmd_run.setToolTip(f"开始发布命令 / 确认当前这一步（{self.hk_texts.get(31, 'Alt+F12')}）")
            self.btn_cmd_clear.setText('取消已输入的内容')
            self.btn_cmd_clear.setToolTip(f"清掉这次发布命令已经输入的内容（{self.hk_texts.get(32, 'Alt+Shift+F12')}）")
            self.btn_cmd_overlay.setText('浮窗显隐')
            self.btn_cmd_overlay.setToolTip(f"显示 / 隐藏半透明浮窗（{self.hk_texts.get(33, 'Ctrl+F12')}）")
            self.lb_cmd_hint.setText(self._cmd_tip())
        for hid, ed in getattr(self, 'qk_edits', {}).items():
            ed.setKeySequence(QKeySequence(self.hk_texts[hid]))

    def _set_digit_keys(self, keys):
        """临时注册小键盘数字热键（望远镜/侦察要用小键盘输入）。注册的键会被本程序吃掉，不会漏进游戏。"""
        want = tuple(sorted(set(keys))) if keys else ()
        if want == self._digit_keys:
            self._numlock_hint()
            return None
        self._digit_keys = want
        self._start_hotkeys()
        self._numlock_hint()

    def _set_mapnum_keys(self, on):
        """地图视角打格号时，临时把主键盘数字 0–9 也注册上（小键盘那条路照旧）。"""
        want = tuple('0123456789') if on else ()
        if want == getattr(self, '_mapnum_keys', ()):
            self._numlock_hint()
            return None
        self._mapnum_keys = want
        self._start_hotkeys()
        self._numlock_hint()

    def _set_char_keys(self, on):
        """侦察输入队友坐标时，临时把 A–Z 和小键盘 0–9 全部注册成热键（打完自动注销）。"""
        want = tuple(sorted(set('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'))) if on else ()
        if want == self._char_keys:
            self._numlock_hint()
            return None
        self._char_keys = want
        self._start_hotkeys()
        self._numlock_hint()

    def _numlock_hint(self):
        """正在等玩家用小键盘输入时，提示 NumLock 状态（关灯时小键盘发的是方向键）。"""
        if not hasattr(self, 'lb_hk'):
            return None
        active = (bool(getattr(self, '_digit_keys', ())) or bool(getattr(self, '_char_keys', ())))
        timer = getattr(self, '_nl_timer', None)
        if timer is not None:
            if active and not timer.isActive():
                timer.start()
            elif not active and timer.isActive():
                timer.stop()
        if not active:
            self.lb_hk.setStyleSheet('color:#7f909f;font-size:11px;')
            self.lb_hk.setText(self._hk_hint())
            return None
        if numlock_on():
            self.lb_hk.setStyleSheet('color:#9fe6f5;font-size:11px;')
            self.lb_hk.setText('正在用小键盘输数字（NumLock 已开）')
            return None
        self.lb_hk.setStyleSheet('color:#ff8f8f;font-size:11px;')
        self.lb_hk.setText('⚠ NumLock 没开：小键盘现在发的是方向键 —— 先按一下 NumLock 再输数字')

    def _start_hotkeys(self):
        old = getattr(self, 'hotkeys', None)
        if old is not None:
            try:
                old.stop()
                old.wait(1000)
            except Exception:
                pass
        self._combos = {}
        self._hid_combo = {}
        bindings = []
        uniq = {}
        for hid, _ck, _d in HOTKEY_DEFS:
            mods, vk = self.hk_specs[hid]
            key = (mods, vk)
            cid = uniq.get(key)
            if cid is None:
                cid = (COMBO_ID + len(uniq))
                uniq[key] = cid
                self._combos[cid] = []
                bindings.append((cid, mods, vk))
            self._combos[cid].append(hid)
            self._hid_combo[hid] = cid
        seen = set(uniq)
        for d in getattr(self, '_digit_keys', ()):
            for item in (((DIGIT_ID + int(d)), 0, numpad_vk(int(d))), ((TOPDIGIT_ID + int(d)), 0, (48 + int(d)))):
                if (item[1], item[2]) in seen:
                    continue
                seen.add((item[1], item[2]))
                bindings.append(item)
        for ch in getattr(self, '_char_keys', ()):
            if ch.isalpha():
                item = (((LETTER_ID + ord(ch)) - 65), 0, ((65 + ord(ch)) - 65))
            elif ch.isdigit():
                item = ((RDIGIT_ID + int(ch)), 0, numpad_vk(int(ch)))
            else:
                continue
            if (item[1], item[2]) in seen:
                continue
            seen.add((item[1], item[2]))
            bindings.append(item)
        for d in getattr(self, '_mapnum_keys', ()):
            item = ((TOPDIGIT_ID + int(d)), 0, (48 + int(d)))
            if (item[1], item[2]) in seen:
                continue
            seen.add((item[1], item[2]))
            bindings.append(item)
        if (getattr(self, '_digit_keys', ()) or getattr(self, '_char_keys', ()) or getattr(self, '_mapnum_keys', ())) and (0, VK_BACK) not in seen:
            bindings.append((BACK_ID, 0, VK_BACK))
        self.hotkeys = Hotkeys(bindings, self)
        self.hotkeys.pressed.connect(self.on_hotkey)
        self.hotkeys.failed.connect(self.on_hotkey_failed)
        self.hotkeys.start()

    def apply_hotkey(self, hid=1):
        ed = getattr(self, 'qk_edits', {}).get(hid)
        if ed is None:
            return None
        seq = ed.keySequence()
        spec = seq_to_hotkey(seq)
        old_text = self.hk_texts[hid]
        if spec is None:
            self.lb_hk.setStyleSheet('color:#ff8f8f;font-size:11px;')
            self.lb_hk.setText('这个键用不了：请用 F1–F24、字母/数字（可加 Ctrl/Shift/Alt）')
            ed.setKeySequence(QKeySequence(old_text))
            return None
        text = seq.toString(QKeySequence.NativeText)
        if text == old_text:
            return None
        for other, other_text in self.hk_texts.items():
            if not other != hid:
                continue
            if not other_text == text:
                continue
            if not ID_SET.get(other) == ID_SET.get(hid):
                continue
            self.lb_hk.setStyleSheet('color:#ff8f8f;font-size:11px;')
            self.lb_hk.setText(f"「{text}」在同一套里已经给「{ID_TITLE.get(other, '别的功能')}」用了，换一个（不同模式之间可以共用同一个键）")
            ed.setKeySequence(QKeySequence(old_text))
            return None
        mods, vk = spec
        self.hk_specs[hid] = (mods, vk)
        self.hk_texts[hid] = text
        for ckey, chid in (('hotkey', 1), ('hotkey_exact', 2), ('hotkey_clear', 3), ('hotkey_overlay', 4), ('r_hotkey', 11), ('r_clear', 12), ('r_overlay', 13), ('r_report', 14), ('s_hotkey', 21), ('s_clear', 22), ('s_overlay', 23), ('c_hotkey', 31), ('c_clear', 32), ('c_overlay', 33), ('b_hotkey', 41), ('b_clear', 42), ('b_overlay', 43)):
            if not chid == hid:
                continue
            self.cfg[ckey] = text
        save_config(self.cfg)
        if hid in (31, 32, 33):
            bad = getattr(self, '_cmd_bad_keys', None)
            if bad is not None:
                bad.discard(hid)
            lbw = getattr(self, 'lb_cmd_warn', None)
            if lbw is not None:
                lbw.setVisible(False)
        if not getattr(self, '_hk_suspended', False):
            self._start_hotkeys()
        self.refresh_hotkey_texts()
        self.recompute()
        plain = (mods == 0 and (not (112 <= vk <= 135)))
        msg = f"当前：{text} · 已保存"
        if plain:
            msg += '（单键会全局占用，游戏里打字也会触发，建议加 Ctrl/Shift）'
        self.lb_hk.setStyleSheet('color:#9fe6f5;font-size:11px;')
        self.lb_hk.setText(msg)

    def on_hotkey_failed(self, hid, err, mods):
        if hid >= COMBO_ID:
            members = (self._combos.get(hid) or [])
            names = ' / '.join((self.hk_texts.get(h, '?') for h in members))
            if any((h in (31, 32, 33) for h in members)):
                bad = getattr(self, '_cmd_bad_keys', None)
                if bad is None:
                    bad = self._cmd_bad_keys = set()
                bad.update((h for h in members if h in (31, 32, 33)))
                lbw = getattr(self, 'lb_cmd_warn', None)
                if lbw is not None:
                    dead = ' / '.join((self.hk_texts.get(h, '?') for h in (31, 32, 33) if h in bad))
                    lbw.setText(f"⚠ 「{dead}」被别的程序占用了（错误码 {err}）：按下它不会到本程序。点上面的三个键位框换成别的组合，或者先用面板上的按钮操作；常见占用者：截图/录屏工具的 F12、语音工具的 F8。")
                    lbw.setVisible(True)
            self.lb_hk.setStyleSheet('color:#ff8f8f;font-size:11px;')
            self.lb_hk.setText(f"「{names}」注册失败（错误码 {err}，通常是被别的程序占用），换一个组合试试")
            self.lb_status.setStyleSheet('color:#ff8f8f;font-size:12px;')
            self.lb_status.setText(f"热键「{names}」被占用：请点面板上的按钮，或改一个热键")
            return None
        if hid == BACK_ID:
            self.lb_status.setStyleSheet('color:#ff8f8f;font-size:12px;')
            self.lb_status.setText(f"Backspace 被别的程序占用（错误码 {err}）—— 输入时暂时删不了错，可用清除键重来")
            return None
        if hid >= LETTER_ID:
            self.lb_status.setStyleSheet('color:#ff8f8f;font-size:12px;')
            self.lb_status.setText(f"字母/小键盘数字键被别的程序占用（错误码 {err}）—— 侦察输入队友坐标需要逐个敲键，先关掉占用它的软件")
            return None
        if hid >= DIGIT_ID:
            self.lb_status.setStyleSheet('color:#ff8f8f;font-size:12px;')
            self.lb_status.setText(f"小键盘数字键 {(hid - 100)} 被别的程序占用（错误码 {err}）—— 关掉占用它的软件后重试")
            return None
        name = self.hk_texts.get(hid, '?')
        self.lb_hk.setStyleSheet('color:#ff8f8f;font-size:11px;')
        self.lb_hk.setText(f"「{name}」注册失败（错误码 {err}，通常是被别的程序占用），换一个组合试试")
        self.lb_status.setStyleSheet('color:#ff8f8f;font-size:12px;')
        self.lb_status.setText(f"热键「{name}」被占用：请点面板上的按钮，或改一个热键")

    def _key_set(self):
        """当前处在哪一套键里：recon / command / scope / drone。"""
        m = getattr(self, 'mode', 'arty')
        if m == 'recon':
            return 'recon'
        if m == 'command':
            return 'command'
        if m == 'barrage':
            return 'bar'
        if getattr(self, 'arty_sub', 'drone') == 'scope':
            return 'scope'
        return 'drone'

    def _resolve_combo(self, cid, prefer=None):
        """一个物理键可能绑了多套动作：优先做当前模式那一套的，浮窗键三套通用。"""
        hids = list((getattr(self, '_combos', {}).get(cid) or []))
        if not hids:
            return None
        cur = self._key_set()
        same = [h for h in hids if ID_SET.get(h) == cur]
        if same:
            if prefer in same:
                return prefer
            return same[0]
        if any((h in OVERLAY_IDS for h in hids)):
            for h in OVERLAY_IDS:
                if not ID_SET.get(h) == cur:
                    continue
                return h
            return hids[0]
        owners = '、'.join((SET_TITLE.get(ID_SET.get(h, ''), '?') for h in hids))
        self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
        self.lb_status.setText(f"这个键只绑在「{owners}」上；现在是「{SET_TITLE.get(cur, cur)}」，这里没给它安排动作（点面板切模式，或换个键）")

    def _mode_hint(self, want):
        """热键按错模式时给一句提示，而不是静默无反应。"""
        if self.mode == want:
            return True
        names = {'arty': '间瞄火力支援', 'recon': '侦察报告', 'command': '发布命令', 'barrage': '呼叫弹幕'}
        here = names.get(self.mode, self.mode)
        there = names.get(want, want)
        self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
        self.lb_status.setText(f"现在是「{here}」模式：这个键只在「{there}」模式下生效（面板顶部可切换）")
        if getattr(self, 'mode', '') == 'command' and hasattr(self, 'lb_cmd_step'):
            self.lb_cmd_step.setStyleSheet('color:#ffd166;font-size:12px;')
            self.lb_cmd_step.setText(f"这个键只在「{there}」模式下生效")
        return False

    def _sub_hint(self, want):
        """同模式下的子模式提示：无人机视角 / 望远镜视角各有一套键。"""
        if getattr(self, 'arty_sub', 'drone') == want:
            return True
        here = '望远镜视角' if self.arty_sub == 'scope' else '无人机视角'
        there = '望远镜视角' if want == 'scope' else '无人机视角'
        self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
        self.lb_status.setText(f"现在是「{here}」子模式：这个键只在「{there}」下生效（面板「间瞄子模式」可切换）")
        return False

    def on_hotkey(self, hid):
        if hid >= COMBO_ID:
            hid = self._resolve_combo(hid)
        elif hid in getattr(self, '_hid_combo', {}):
            hid = self._resolve_combo(self._hid_combo[hid], prefer=hid)
        if hid is None:
            return None
        if hid == BACK_ID:
            if self.mode == 'command':
                self.cmd_backspace()
                return None
            self.backspace()
            return None
        if hid >= TOPDIGIT_ID:
            ch = chr(((ord('0') + hid) - TOPDIGIT_ID))
            if self.mode == 'command':
                self.cmd_digit(ch)
                return None
            if self.mode == 'barrage':
                self.bar_digit(ch)
                return None
            st = (self.recon or {}).get('state')
            if st in ('digit', 'dist', 'digit4', 'digit4_ok', 'type', 'item', 'common', 'rpt_pick', 'mark_mode', 'upd_pick', 'clr_choose', 'clr_pick', 'mcoord'):
                self.recon_digit(ch)
                return None
            if (self.tel or {}).get('state') in ('digit', 'dist'):
                self.tel_digit(ch)
                return None
            self.recon_char(ch)
            return None
        if hid >= RDIGIT_ID:
            if self.mode == 'command':
                self.cmd_digit(chr(((ord('0') + hid) - RDIGIT_ID)))
                return None
            self.recon_char(chr(((ord('0') + hid) - RDIGIT_ID)))
            return None
        if hid >= LETTER_ID:
            if self.mode == 'command':
                self.cmd_char(chr(((ord('A') + hid) - LETTER_ID)))
                return None
            self.recon_char(chr(((ord('A') + hid) - LETTER_ID)))
            return None
        if hid >= DIGIT_ID:
            if self.mode == 'command':
                self.cmd_digit(chr(((ord('0') + hid) - DIGIT_ID)))
                return None
            if self.mode == 'barrage':
                self.bar_digit(chr(((ord('0') + hid) - DIGIT_ID)))
                return None
            st = (self.recon or {}).get('state')
            if st in ('digit', 'dist', 'digit4', 'digit4_ok', 'type', 'item', 'common', 'rpt_pick', 'mark_mode', 'upd_pick', 'clr_choose', 'clr_pick', 'mcoord'):
                self.recon_digit(chr(((ord('0') + hid) - DIGIT_ID)))
                return None
            self.tel_digit(chr(((ord('0') + hid) - DIGIT_ID)))
            return None
        if hid in (41, 42, 43):
            if not self._mode_hint('barrage'):
                return None
            if hid == 41:
                self.bar_step()
                return None
            if hid == 42:
                self.bar_clear('已清除呼叫弹幕')
                return None
            self.toggle_overlay()
            return None
        if hid in (31, 32, 33):
            if not self._mode_hint('command'):
                return None
            if hid == 31:
                self.cmd_step()
                return None
            if hid == 32:
                self.cmd_reset('已取消已输入的内容')
                return None
            self.toggle_overlay()
            return None
        if hid in (11, 12, 13, 14):
            if not self._mode_hint('recon'):
                return None
            if hid == 11:
                self.recon_step()
                return None
            if hid == 12:
                self.recon_clear_menu()
                return None
            if hid == 14:
                self.recon_report()
                return None
            self.toggle_overlay()
            return None
        if hid in (21, 22, 23):
            if not self._mode_hint('arty') or not self._sub_hint('scope'):
                return None
            if hid == 21:
                self.tel_step()
                return None
            if hid == 22:
                self.clear_pending()
                return None
            self.toggle_overlay()
            return None
        if not hid in (1, 2, 3, 4) or not self._mode_hint('arty') or not self._sub_hint('drone'):
            return None
        if hid == 1:
            self.start_ocr('quick')
            return None
        if hid == 2:
            self.start_ocr('exact')
            return None
        if hid == 3:
            self.clear_pending()
            return None
        self.toggle_overlay()
        return None

    def _grid_range_text(self, m):
        """这张图的合法格号范围（A1 – 最后一列的字母 + 行数）。"""
        cols = max(1, int(math.ceil((m.spanx / core.KP_M))))
        rows = max(1, int(math.ceil((m.spanz / core.KP_M))))
        return f"这张图（{m.label}）的网格范围：A1 – {core.col_letter((cols - 1))}{rows}"

    def _locate_hint(self, m):
        gx, gz = m.spanx / 2.0, m.spanz / 2.0
        return f"输入网格坐标（例 {m.grid_at(gx, gz, 4)}；只输 3 级 {m.grid_at(gx, gz, 3)} 也行）\n{self._grid_range_text(m)}"

    def _locate_warn(self, m, txt, why):
        """坐标不可用：面板红字 + 弹窗，然后继续弹输入框让玩家改。"""
        why = str((why or '')).strip()
        if why.startswith(txt):
            why = why[len(txt):].strip()
        self.lb_status.setStyleSheet('color:#ff8f8f;font-size:12px;')
        self.lb_status.setText(f"「{txt}」定位不了：{why}")
        title = '坐标不在这张地图上' if '不在这张地图上' in why else '坐标写得不对'
        QMessageBox.warning(self, title, f"「{txt}」定位不了，所以没有移动地图。\n\n原因：{why}\n{self._grid_range_text(m)}。\n\n改一下坐标再按确定；如果这个坐标是从别的地图抄来的，先在面板「地图」里换成那张图。")

    def locate_dialog(self):
        """地图左上「查坐标」：输 4 级坐标直接定位（3 级也行）。

    输错格式、或者输了一个当前地图上没有的格号，都直接弹窗提醒，
    并把刚才的文字留在输入框里让玩家改，不会像以前那样只是静默什么都不做。
    """
        if not getattr(self.view, 'map', None):
            QMessageBox.warning(self, '查坐标', '地图还没加载好，稍等一下再试。')
            return None
        m = self.view.map
        txt = ''
        self._set_hotkeys_suspended(True)
        try:
            while True:
                txt, ok = QInputDialog.getText(self, '按坐标定位', self._locate_hint(m), text=txt)
                if not ok:
                    return None
                txt = (txt or '').strip()
                if not txt:
                    return None
                try:
                    rx, rz = m.locate(txt)
                except core.ParseError as e:
                    self._locate_warn(m, txt, str(e))
                else:
                    g4 = m.grid_at(rx, rz, 4)
                    h = m.h_at(rx, rz)
                    self.view.focus_point(rx, rz)
                    self.view.set_pin((rx, rz), f"{g4} · {h:.0f} m")
                    self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
                    self.lb_status.setText(f"已定位 {g4} · 高程 {h:.0f} m（紫十字为查询点；左键点地图可设阵地）")
                continue
        finally:
            self._set_hotkeys_suspended(False)

    @staticmethod
    def _fmt_elapsed(sec):
        """秒 -> 00:00:00（小时:分钟:秒）。"""
        s = max(0, int(sec))
        return f"{(s // 3600):02d}:{((s % 3600) // 60):02d}:{(s % 60):02d}"

    def on_timer_clicked(self):
        """同一个按钮：没在计时就开始，正在计时就结束（不能暂停）。"""
        if self.timer_start is None:
            self.timer_start = time.monotonic()
            self._timer_clear.stop()
            self.view.set_timer_state(True)
            self.overlay.set_timer('00:00:00', 'run')
            self._timer.start()
            print('[timer] 开始计时 00:00:00', flush=True)
            self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
            self.lb_status.setText('计时开始 00:00:00 · 时间显示在浮窗第一行；再点地图上的「结束」停止（不能暂停）')
            return None
        el = self._fmt_elapsed((time.monotonic() - self.timer_start))
        self.timer_start = None
        self._timer.stop()
        self.view.set_timer_state(False)
        self.overlay.set_timer(el, 'stop')
        self._timer_clear.start(6000)
        print(f"[timer] 结束计时，用时 {el}", flush=True)
        self.lb_status.setStyleSheet('color:#ffd166;font-size:12px;')
        self.lb_status.setText(f"计时结束：用时 {el}（浮窗上保留 6 秒）")

    def _tick_timer(self):
        if self.timer_start is None:
            return None
        self.overlay.set_timer(self._fmt_elapsed((time.monotonic() - self.timer_start)), 'run')

    def _clear_timer_text(self):
        if self.timer_start is None:
            self.overlay.set_timer('')
            return None

    def timer_reset(self):
        """停掉并收起计时（退出时用）。"""
        try:
            self._timer.stop()
            self._timer_clear.stop()
        except Exception:
            pass
        self.timer_start = None
        try:
            self.view.set_timer_state(False)
            self.overlay.set_timer('')
        except Exception:
            return None
        else:
            return None

    def on_grid_toggled(self, on):
        self.view.show_grid = bool(on)
        self.view.update()

    def on_flag_notice(self, text):
        """地图上的旗点面板发来的提示：记进 run.log，顺便写状态栏。"""
        print(f"[flags] {text}", flush=True)
        try:
            self.lb_status.setStyleSheet('color:#9fe6f5;font-size:12px;')
            self.lb_status.setText(text)
        except Exception:
            return None

    def on_hd_ready(self):
        """高清底图解好了：小地图如果正开着，换成清晰的这张重新画。"""
        try:
            if self.minimap.isVisible() and self.view.img is not None:
                self.minimap.img = self.view.img
                self.minimap.update()
                return None
            return None
        except Exception:
            return None

    def show_main_window(self):
        """把主界面叫回来（浮窗双击 / 右键菜单用）。"""
        self.show()
        self.setWindowState((self.windowState() & (~Qt.WindowMinimized)))
        self.raise_()
        self.activateWindow()
        if self.overlay.isVisible():
            self.overlay.raise_()
        self.lb_status.setText('主界面已恢复')

    def toggle_overlay(self):
        if self.overlay.isVisible():
            self.overlay.hide()
            return None
        self.overlay.show()
        self.overlay.raise_()

    def on_overlay_moved(self, x, y):
        self.cfg['overlay'] = [x, y]
        save_config(self.cfg)

    def quit_app(self):
        """红色「退出程序」：注销热键、关浮窗，然后彻底结束进程。"""
        self._shutdown('退出')
        QApplication.quit()

    def _shutdown(self, reason=''):
        if getattr(self, '_shutting', False):
            return None
        self._shutting = True
        try:
            print(f"[app] {reason}", flush=True)
        except Exception:
            pass
        try:
            if self.hotkeys is not None:
                self.hotkeys.stop()
                self.hotkeys.wait(800)
        except Exception:
            pass
        try:
            if self.worker and self.worker.isRunning():
                # 识图线程可能正等在 AI 接口上（实测 4 s、最长 30 s），等一小会儿；
                # 还不回来的话就把它摘下来不管了 —— 不能让它随窗口一起析构
                # （QThread 在运行中被销毁 = 0xC0000409，--hotkeytest 踩过）。
                self.worker.stop()
                if not self.worker.wait(1500):
                    print('[app] 识图线程还没收尾：它随进程结束，这次识别结果作废', flush=True)
        except Exception:
            pass
        try:
            self.view.wait_decodes(3000)
        except Exception:
            pass
        try:
            self._timer.stop()
            self._timer_clear.stop()
            self.timer_start = None
        except Exception:
            pass
        try:
            if self.drill is not None:
                self.drill.close()
                self.drill = None
        except Exception:
            pass
        try:
            self.minimap.hide()
            self.minimap.close()
        except Exception:
            pass
        try:
            self.overlay.hide()
            self.overlay.close()
        except Exception:
            pass
        try:
            save_config(self.cfg)
        except Exception:
            return None
        else:
            return None

    def closeEvent(self, ev):
        self._shutdown('关闭窗口')
        super().closeEvent(ev)

class _SafeStream:
    """包一层 stdout/stderr：写日志永远不该把程序弄崩。

    踩过两次：①控制台是 GBK（cp936）时，日志里一个 ² 就抛 UnicodeEncodeError，
    在 __init__ 里直接把启动打掉（双击 = 没反应）；②重定向到文件/管道时句柄
    偶尔报 OSError 22。这里两种都吞掉，只丢一行日志。
    """
    def __init__(self, raw):
        self._raw = raw

    def write(self, s):
        try:
            return self._raw.write(s)
        except (OSError, ValueError, UnicodeError):
            return len(s)

    def flush(self):
        try:
            self._raw.flush()
        except Exception:
            return None

    def __getattr__(self, name):
        return getattr(self._raw, name)

def _prune_stale_mei(keep_minutes=30.0):
    '收拾被强行结束时留下的自解压目录。\n\nonefile 正常退出会删掉 %TEMP%\\_MEIxxxx；被任务管理器 / Stop-Process 干掉时\n目录会留在原地 —— 1.8.8 起包里带约 444 MB 地图，攒几次就是好几个 GB。\n只删「自己身上带 mapsdb\\_db.json（= 本程序的包）且闲置超过 keep_minutes」的目录，\n正在用的那个（sys._MEIPASS）永远跳过，别的程序的临时目录也不碰。\n'
    import os
    import shutil
    cur = getattr(sys, '_MEIPASS', '')
    if not cur:
        return 0
    root = os.path.dirname(os.path.abspath(cur))
    now = time.time()
    n = 0
    try:
        names = os.listdir(root)
    except OSError:
        return 0
    for name in names:
        if not name.lower().startswith('_mei'):
            continue
        p = os.path.join(root, name)
        if os.path.abspath(p) == os.path.abspath(cur):
            continue
        if not os.path.exists(os.path.join(p, 'mapsdb', '_db.json')) and not os.path.exists(os.path.join(p, 'data', 'digit_bank_seed.npz')):
            continue
        try:
            if (now - os.path.getmtime(p)) < (keep_minutes * 60.0):
                continue
            shutil.rmtree(p, ignore_errors=True)
            n += 1
        except OSError:
            pass
    return n

def _boot_logging():
    """打包成单文件（无控制台）时：stdout/stderr 与崩溃堆栈都落盘，
    这样双击运行出问题时还能在 run.log 里看到原因，而不是「点了没反应」。"""
    for name in ('stdout', 'stderr'):
        s = getattr(sys, name, None)
        if s is None:
            continue
        try:
            s.reconfigure(errors='replace')
        except Exception:
            pass
        setattr(sys, name, _SafeStream(s))
    if not paths.frozen():
        return None
    try:
        log = open(paths.log_path(), 'a', encoding='utf-8', buffering=1)
    except Exception:
        return None
    else:
        log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 · exe={sys.executable} · 配置={paths.config_path()} =====\n")
        if sys.stdout is None:
            sys.stdout = log
        if sys.stderr is None:
            sys.stderr = log
        import threading
        def _prune_job():
            try:
                n = _prune_stale_mei()
                if n:
                    print(f"[boot] 清掉 {n} 个上次强行退出留下的解压目录", flush=True)
                    return None
            except Exception:
                return None
            else:
                return None
        threading.Thread(target=_prune_job, daemon=True).start()
    _err_seen = {}
    _err_shown = [0]

    def hook(etype, value, tb):
        import traceback as _tb
        sig = '%s: %s' % (getattr(etype, '__name__', etype), str(value)[:200])
        n = _err_seen.get(sig, 0) + 1
        _err_seen[sig] = n
        try:
            log.write(''.join(_tb.format_exception(etype, value, tb)))
            log.flush()
        except Exception:
            pass
        # 同一个错误会一帧一帧地重复触发（2026-11-14 迁到 D:\Aegis 试跑时，
        # paintEvent 每重绘一次就弹一次「程序遇到问题」，几十个模态框叠起来，
        # 程序看起来就是卡死）。同一签名只弹一次，并且总共最多弹 3 次；
        # 其余全部只写 run.log，界面绝不被对话框堵住。
        if n > 1 or _err_shown[0] >= 3:
            return None
        _err_shown[0] += 1
        try:
            QMessageBox.critical(None, '出错了', f"程序遇到问题：\n{str(value)[:600]}\n\n详细信息已写入：\n{paths.log_path()}\n\n（同一个错误只弹这一次，之后只写日志，不会再一直挡住界面。）")
        except Exception:
            return None
        else:
            return None

    sys.excepthook = hook

def _single_instance(app, args):
    """已经开着一个就不要再开第二个：两个实例会互相抢全局热键（RegisterHotKey 1409），
    第二个实例看起来"热键失灵"，很容易被误判成程序坏了。自测模式不受限制。"""
    if ({'--arctest', '--cleartest', '--dronetest', '--drilltest', '--e2e', '--hotkeytest', '--flagtest', '--locatetest', '--lostest', '--maptest', '--recontest', '--shotdesk'} & set(args)):
        return True
    try:
        from PySide6.QtCore import QSharedMemory
        lock = QSharedMemory('SquadArtyCalc-single-instance')
        if not lock.create(1):
            QMessageBox.information(None, '已经在运行', """Squad 间瞄火力助手已经开着了，请用任务栏/Alt+Tab 切到那个窗口。
（两个同时开的话，全局热键会被其中一个独占，另一个会提示注册失败。）""")
            return False
        app._single_lock = lock
    except Exception:
        return True
    else:
        return True

def main():
    global SELF_TEST
    _boot_logging()
    app = QApplication(sys.argv)
    args = sys.argv[1:]
    SELF_TEST = bool(({'--arctest', '--cleartest', '--dronetest', '--cmdrealtest', '--cmdtest', '--drilltest', '--e2e', '--hotkeytest', '--flagtest', '--locatetest', '--lostest', '--maptest', '--recontest', '--shotdesk'} & set(args)))
    if SELF_TEST:
        # 自测只改内存里的 cfg：别把测试用的识别区域 / 子模式写进你的 config.json
        globals()['save_config'] = (lambda *a, **k: None)
    if not _single_instance(app, args):
        return 0
    cfg = load_config()
    win = MainWindow(cfg)
    win.show()
    if ({'--arctest', '--cleartest', '--dronetest', '--cmdrealtest', '--cmdtest', '--drilltest', '--e2e', '--hotkeytest', '--flagtest', '--locatetest', '--lostest', '--maptest', '--recontest', '--shotdesk'} & set(args)):
        win.select_map('yehorivka')
        if ({'--cmdtest', '--cmdrealtest'} & set(args)):
            win.set_mode('command')
        else:
            win.set_mode('recon' if ({'--recontest', '--maptest'} & set(args)) else 'arty')

    def _sk(tag, name, cond):
        """自测断言。以前这些测试只把期望值 print 出来，对不对全靠人眼看、退出码恒 0；
        现在失败的会登记进 SELF_FAILS，main() 退场时返回 1。"""
        print(tag, 'ok' if cond else 'FAIL', name)
        if not cond:
            self_fail('%s:%s' % (str(tag).lower(), name))
        return bool(cond)

    if '--e2e' in args:
        win.select_weapon('Mortar#default')
        win.set_arc('auto')
        win.gun = (3083.3, 3183.3)
        win.view.gun = win.gun
        tgt_grid = win.map.grid_at(3603.3, 2923.3)
        win.set_target_grid(tgt_grid)
        win.recompute()
        print('e2e target grid', tgt_grid)
        def shot():
            win.grab().save(str((HERE / 'shot_main.png')))
            win.overlay.grab().save(str((HERE / 'shot_overlay.png')))
            print('E2E gun', win.vals['gun'].text(), 'tgt', win.vals['tgt'].text(), 'brg', win.vals['brg'].text(), 'mil', win.vals['mil'].text())
            print('overlay note:', win.overlay.note)
            _sk('E2E', 'gun K11-4-3', win.vals['gun'].text().strip() == 'K11-4-3')
            _sk('E2E', 'tgt M10-1-7', win.vals['tgt'].text().strip() == 'M10-1-7')
            _sk('E2E', 'brg 63.9', win.vals['brg'].text().strip() == '63.9°')
            _sk('E2E', 'mil 1353.09', win.vals['mil'].text().strip() == '1353.09 mil')
            _sk('E2E', 'overlay 22.0 s', '飞行 22.0 s' in (win.overlay.note or ''))
            app.quit()

        QTimer.singleShot(1500, shot)
    if '--arctest' in args:
        # 低抛/高抛切换自测：同一组阵地-目标上依次切三档，再把迫击炮强制成低抛
        # 看它是否老老实实报错（而不是偷偷给高抛的密位），最后截图。
        win.gun = (3083.3, 3183.3)
        win.view.gun = win.gun
        win.set_target_grid(win.map.grid_at(3603.3, 2923.3))
        win.select_weapon('M1064M121#impact')
        _got, _stt0 = {}, {}
        for _k in ('auto', 'low', 'high'):
            win.set_arc(_k)
            _res = win.sol or {}
            _sol = _res.get('sol')
            _got[_k] = win.vals['mil'].text() if _sol else 'ERR'
            _stt0[_k] = win.lb_status.text()
            print('ARC', _k, '->', _got[_k],
                  '|', _res.get('solName') or '-', '|', win.lb_status.text()[:70])
        _sk('ARC', 'M1064 auto 81.85', _got['auto'] == '81.85°')
        # 0.52.0：射界按 pak 的 PitchInterval（M1064 炮管 50–85°），低角 6° 不在行程里，
        # 低抛这一档应该报错而不是给出一个打不出来的 6.01°。
        _sk('ARC', 'M1064 low 超出射界', _got['low'] == 'ERR')
        _sk('ARC', 'M1064 low 提示 50–85°', '50–85°' in (_stt0.get('low') or ''))
        _sk('ARC', 'M1064 high 81.85', _got['high'] == '81.85°')
        win.select_weapon('Mortar#default')
        _got, _ovl, _stt = {}, {}, {}
        for _k in ('low', 'high', 'auto'):
            win.set_arc(_k)
            _res = win.sol or {}
            _got[_k] = win.vals['mil'].text() if _res.get('sol') else 'ERR'
            _ovl[_k] = win.overlay.note
            _stt[_k] = win.lb_status.text()
            print('ARC mortar', _k, '->', _got[_k],
                  '|', win.lb_status.text()[:90], '| overlay:', win.overlay.note)
        _sk('ARC', 'mortar low ERR', _got['low'] == 'ERR')
        _sk('ARC', 'mortar low 只有高抛解', (_ovl.get('low') or '') == '低抛打不了 · 只有高抛解')
        _sk('ARC', 'mortar low errShort 提示', '这组距离只有高抛解' in (_stt.get('low') or ''))
        _sk('ARC', 'mortar high 1353.09', _got['high'] == '1353.09 mil')
        _sk('ARC', 'mortar auto 1353.09', _got['auto'] == '1353.09 mil')
        def arc_shot():
            win.scroll.ensureWidgetVisible(win.gb_alg, 0, 10)
            QTimer.singleShot(500, arc_grab)

        def arc_grab():
            p = HERE / 'shot_arc_panel.png'
            win.panel.grab().save(str(p))
            print('ARC panel', p.exists(), win.arc, win.btns_arc[win.arc].isChecked())
            app.quit()

        QTimer.singleShot(1200, arc_shot)
    if '--drilltest' in args:
        gun = (3083.3, 3183.3)
        win.gun = gun
        win.view.gun = gun
        win.view.update()
        coord = win.map.grid_at((gun[0] + 480), (gun[1] + 120))
        dpr = QGuiApplication.primaryScreen().devicePixelRatio()
        win.drill = DrillWindow(win.cfg['region'], coord, dpr)
        win.drill.show()
        win.drill.raise_()
        def go():
            try:
                img = vision.grab(tuple(win.cfg['region']))
                img.save(str((HERE / 'shot_region.png')))
                got, raw, ms = vision.ask(img, win.ed_key.text().strip())
                print('drill expect', coord, 'got', repr(got), ms, 'ms raw', raw[:60])
                win.on_ocr_ok((got or ''), raw, ms)
            except Exception as e:
                print('drill error', e)
            win.grab().save(str((HERE / 'shot_main.png')))
            win.overlay.grab().save(str((HERE / 'shot_overlay.png')))
            print('drill values', win.vals['gun'].text(), win.vals['tgt'].text(), win.vals['brg'].text(), win.vals['mil'].text())
            app.quit()

        QTimer.singleShot(1200, go)
    if '--hotkeytest' in args:
        gun = (3083.3, 3183.3)
        win.gun = gun
        win.view.gun = gun
        win.recompute()
        coord = win.map.grid_at((gun[0] - 420), (gun[1] + 300))
        dpr = QGuiApplication.primaryScreen().devicePixelRatio()
        win.drill = DrillWindow(win.cfg['region'], coord, dpr)
        win.drill.show()
        win.drill.raise_()
        def press():
            user32 = ctypes.windll.user32
            mods, vk = win.hk_specs[1]
            hold = []
            if mods & 2:
                hold.append(17)
            if mods & 4:
                hold.append(16)
            if mods & 1:
                hold.append(18)
            for m in hold:
                user32.keybd_event(m, 0, 0, 0)
            user32.keybd_event(vk, 0, 0, 0)
            user32.keybd_event(vk, 0, 2, 0)
            for m in reversed(hold):
                user32.keybd_event(m, 0, 2, 0)
            print(f"sent {win.hk_text} (mods={mods:#x} vk={vk:#x}), drill shows", coord)

        def report():
            print('hotkey OCR:', getattr(win, 'last_ocr', None), '| status:', win.lb_status.text(), '| mil:', win.vals['mil'].text())
            app.quit()

        QTimer.singleShot(1800, press)
        QTimer.singleShot(8000, report)
    if '--recontest' in args:
        win.select_map('yehorivka')
        win.set_mode('recon')
        win.set_recon_sub('drone')
        win._start_hotkeys = (lambda: None)
        win._set_digit_keys = (lambda k: None)
        win._set_char_keys = (lambda on: None)
        win.recon_src = 'cmd'
        win.cfg['recon_src'] = 'cmd'
        win._sync_rsrc_buttons()
        if not win.cfg.get('r_region_tgt'):
            win.cfg['r_region_tgt'] = (win.cfg.get('region') or [100, 100, 200, 60])
        factions = ((win.recon_targets or {}).get('factions') or [])
        if not win.cfg.get('r_faction') and factions:
            win.cfg['r_faction'] = factions[0].get('id')
            win._refresh_faction_combo()
        dbpath = HERE / 'targets_selftest.db'
        try:
            win.target_db.close()
        except Exception:
            pass
        if dbpath.exists():
            dbpath.unlink()
        win.target_db = targetsdb.TargetDB(dbpath)
        seq = [('grid', 'K12-1-8')]
        win.cfg.pop('d_region_az_cmd', None)
        win.cfg.pop('d_region_az_scout', None)
        def fake_job(region, kind, busy, note, flow='tel', **_kw):
            win.ocr_kind = kind
            win.ocr_flow = flow
            if not seq:
                return False
            text = seq.pop(0)[1]
            QTimer.singleShot(80, (lambda: win.on_ocr_ok(text, 'local', 0)))
            return True

        win._ocr_job = fake_job
        phase = {'p': 'tgt'}
        def rtick():
            r = win.recon or {}
            st = r.get('state')
            if phase['p'] == 'tgt':
                win.recon_step()
                if (win.recon or {}).get('state') == 'mark_mode':
                    win.recon_digit('1')
                phase['p'] = 'mini'
            elif phase['p'] == 'mini' and st == 'tgt_mini':
                tg = ((win.recon or {}).get('target') or (0.0, 0.0))
                win.on_minimap_picked((tg[0] + 4.0), (tg[1] + 3.0))
                phase['p'] = 'type'
            elif phase['p'] == 'type' and st == 'type':
                win.recon_digit('1')
                phase['p'] = 'item'
            elif phase['p'] == 'item' and st == 'item':
                win.recon_digit('1')
                phase['p'] = 'report'
            elif phase['p'] == 'report' and st == 'saved':
                win.recon_report()
                phase['p'] = 'pick'
            elif phase['p'] == 'pick' and st == 'rpt_pick':
                win.recon_digit('1')
                phase['p'] = 'buddy'
            elif phase['p'] == 'buddy' and st == 'rpt_buddy':
                for ch in 'B11256':
                    win.recon_buddy_char(ch)
                    continue
                phase['p'] = 'done'
            if phase['p'] == 'done' and st == 'done':
                win.panel.grab().save(str((HERE / 'shot_recon_panel.png')))
                win.overlay.grab().save(str((HERE / 'shot_recon_overlay.png')))
                last = win.recon_last or {}
                print('RECON tg', last.get('tg'), '| mate', last.get('mate'), '| mateview', ('%.0f° · %.0f m' % ((last.get('brg') or 0), (last.get('bdist') or 0))))
                _sk('RECON', 'tg K12-1-8-5', last.get('tg') == 'K12-1-8-5')
                _sk('RECON', 'mate B11-2-5-6', last.get('mate') == 'B11-2-5-6')
                _sk('RECON', 'mateview 96° 2607 m',
                    ('%.0f° · %.0f m' % ((last.get('brg') or 0), (last.get('bdist') or 0))) == '96° · 2607 m')
                print('RECON clip', QGuiApplication.clipboard().text())
                print('RECON db', win.target_db.count('yehorivka'), win.target_db.recent('yehorivka', 1))
                print('RECON status', win.lb_status.text())
                win.mode, win.ocr_flow, win.recon = 'arty', 'recon', None
                try:
                    win.on_ocr_ok('K13-2-2-2', 'local', 0)
                    print('FLOW ok', win.ocr_flow, 'target', win.map.grid_at(*win.target) if win.target else None)
                except Exception as e:
                    print('FLOW CRASH', type(e).__name__, e)
                app.quit()
                return None
            QTimer.singleShot(450, rtick)

        QTimer.singleShot(900, rtick)
        QTimer.singleShot(30000, (lambda: (print('RECON 自测超时'), app.quit())))
    if '--maptest' in args:
        win.select_map('yehorivka')
        win.set_mode('recon')
        win.set_recon_sub('map')
        win._start_hotkeys = (lambda: None)
        win._set_digit_keys = (lambda k: None)
        win._set_char_keys = (lambda on: None)
        win._set_mapnum_keys = (lambda on: None)
        win.cfg['r_faction'] = 'RGF'
        win._refresh_faction_combo()
        dbpath = HERE / 'targets_maptest.db'
        try:
            win.target_db.close()
        except Exception:
            pass
        if dbpath.exists():
            dbpath.unlink()
        win.target_db = targetsdb.TargetDB(dbpath)
        mphase = {'p': 'mark'}
        def _mtop(ch):
            win.on_hotkey((TOPDIGIT_ID + int(ch)))

        def _mletter(ch):
            win.on_hotkey(((LETTER_ID + ord(ch)) - 65))

        def mtick():
            r = win.recon or {}
            st = r.get('state')
            if mphase['p'] == 'mark':
                win.on_hotkey(11)
                if (win.recon or {}).get('state') == 'mark_mode':
                    _mtop('1')
                mphase['p'] = 'type_coord'
            elif mphase['p'] == 'type_coord' and st == 'mcoord':
                _mletter('K')
                _mtop('1')
                _mtop('2')
                _mtop('1')
                _mtop('8')
                _mtop('8')
                mphase['p'] = 'confirm'
            elif mphase['p'] == 'confirm' and st == 'mcoord':
                print('MAP typed', (win.recon or {}).get('buf'), '| tg', win.rvals['tg'].text())
                _sk('MAP', 'typed K12188', ((win.recon or {}).get('buf') or '').strip() == 'K12188')
                win.on_hotkey(11)
                mphase['p'] = 'cat'
            elif mphase['p'] == 'cat' and st == 'type':
                _mtop('1')
                mphase['p'] = 'item'
            elif mphase['p'] == 'item' and st == 'item':
                win.on_hotkey((DIGIT_ID + 1))
                mphase['p'] = 'report'
            elif mphase['p'] == 'report' and st == 'saved':
                print('MAP saved', r.get('uid'), r.get('name'), r.get('grid'), '| sub', r.get('sub'))
                _sk('MAP', 'saved T-72B3 K12-1-8-8',
                    (r.get('name'), r.get('grid')) == ('T-72B3', 'K12-1-8-8'))
                print('MAP clip', QGuiApplication.clipboard().text())
                win.on_hotkey(14)
                mphase['p'] = 'pick'
            elif mphase['p'] == 'pick' and st == 'rpt_pick':
                win.on_hotkey((DIGIT_ID + 1))
                mphase['p'] = 'buddy'
            elif mphase['p'] == 'buddy' and st == 'rpt_buddy':
                _mletter('B')
                _mtop('1')
                _mtop('1')
                _mtop('2')
                _mtop('5')
                _mtop('6')
                mphase['p'] = 'done'
            if mphase['p'] == 'done' and st == 'done':
                win.panel.grab().save(str((HERE / 'shot_map_panel_selftest.png')))
                win.overlay.grab().save(str((HERE / 'shot_map_overlay_selftest.png')))
                last = win.recon_last or {}
                print('MAP report', ('%.0f° · %.0f m' % ((last.get('brg') or 0), (last.get('bdist') or 0))), '| mate', last.get('mate'))
                _sk('MAP', 'report 96° 2601 m',
                    ('%.0f° · %.0f m' % ((last.get('brg') or 0), (last.get('bdist') or 0))) == '96° · 2601 m')
                print('MAP clip2', QGuiApplication.clipboard().text())
                print('MAP db', win.target_db.count('yehorivka'), win.target_db.recent('yehorivka', 1))
                print('MAP status', win.lb_status.text())
                try:
                    win.target_db.close()
                except Exception:
                    pass
                if dbpath.exists():
                    dbpath.unlink()
                app.quit()
                return None
            QTimer.singleShot(450, mtick)

        QTimer.singleShot(900, mtick)
    if '--lostest' in args:
        # 弹道遮挡自测（2026-11-14 新增）。用户的要求是：弹道被建筑 / 桥梁 / 地形挡住时，
        # 小地图要显示「被挡住的那片打不到的区域」，而不是只显示修正后的目标区域。
        # 这里用真实地图数据分两段验：
        #   ① 纯计算：随机搜一组必被挡的射击，检查挡点真的落在炮位→目标连线上、
        #      被挡区域的四边形和地物四角都有，通畅的射击不误报；
        #   ② 界面：灌进主窗口看状态行 / 浮层文案，再弹小地图截图，数红色斜线像素，
        #      换一组通畅的目标后红区必须消失（不能残留上一次的判定）。
        win.set_mode('arty')
        win._start_hotkeys = (lambda: None)
        win._set_digit_keys = (lambda k: None)
        win._set_char_keys = (lambda on: None)
        win._set_mapnum_keys = (lambda on: None)
        win.tel = None
        win.tel_warn = ''
        _los_errs = []
        _los_calls = [0]
        _pl_orig = MiniMap._paint_los

        def _pl_wrap(_self, _p, _inner):
            _los_calls[0] += 1
            return _pl_orig(_self, _p, _inner)

        MiniMap._paint_los = _pl_wrap

        def _los_hook(_t, _v, _tb):
            _los_errs.append(''.join(traceback.format_exception(_t, _v, _tb)))
            traceback.print_exception(_t, _v, _tb)
            return None

        sys.excepthook = _los_hook
        _md = win.map
        try:
            _lm = win.los_store.get(_md)
        except Exception as _e:
            print('LOS pack error', _e, flush=True)
            _lm = None
        _sk('LOS', f'{_md.slug} 遮挡包已内置', bool(_lm and getattr(_lm, 'count', 0)))
        _sk('LOS', '遮挡包里有树', bool(_lm and getattr(_lm, 'tcount', 0)))
        _wz = None
        for _c in win.weapons:
            if _c.get('key') == 'ZiS3#default':
                _wz = _c
                break
        _sk('LOS', '武器表里有 ZiS-3', _wz is not None)
        if _wz is None:
            _wz = win.weapons[0]
        _shot = {}

        def _trace_at(_gun, _tgt):
            _hg = _md.h_at(_gun[0], _gun[1])
            _ht = _md.h_at(_tgt[0], _tgt[1])
            _res = core.evaluate(_wz, _gun, _tgt, _hg, _ht, 'auto')
            _sol = _res.get('sol') or _res.get('alt')
            if not _sol:
                return (None, _hg, None)
            return (los.trace(_md, _lm, _wz, _gun, _tgt, _hg, _sol), _hg, _sol)

        def _find(_blocked=True, _tries=1200):
            _rnd = random.Random(20261114)
            _any = None
            for _i in range(_tries):
                _gx = _rnd.uniform(0.06, 0.94) * _md.spanx
                _gz = _rnd.uniform(0.06, 0.94) * _md.spanz
                _a = _rnd.uniform(0.0, 6.283185307179586)
                _d = _rnd.uniform(380.0, 1500.0)
                _tx = _gx + math.cos(_a) * _d
                _tz = _gz + math.sin(_a) * _d
                if not (10.0 < _tx < _md.spanx - 10.0 and 10.0 < _tz < _md.spanz - 10.0):
                    continue
                _info, _hg, _sol = _trace_at((_gx, _gz), (_tx, _tz))
                if _info is None:
                    continue
                if not _blocked:
                    if not _info.get('blocked'):
                        return ((_gx, _gz), (_tx, _tz), _info, _hg, _sol)
                    continue
                if not _info.get('blocked'):
                    continue
                if _info.get('kind') == 'structure' and _info.get('foot'):
                    return ((_gx, _gz), (_tx, _tz), _info, _hg, _sol)
                if _any is None:
                    _any = ((_gx, _gz), (_tx, _tz), _info, _hg, _sol)
            return _any

        def _diff_px(_a, _b):
            """两张图有多少个像素不一样（隔 2 抽样）。

            不能按「红色分量高」数：这张底图本身就是红棕色的土，天然有一堆红像素，
            数出来跟斜线区一个量级。改成同一块视野开 / 关遮挡层各截一张做差——
            差值只可能来自斜线区和挡点标记。"""
            _n = 0
            _w = min(_a.width(), _b.width())
            _h = min(_a.height(), _b.height())
            for _y in range(0, _h, 2):
                for _x in range(0, _w, 2):
                    _p = _a.pixelColor(_x, _y)
                    _q = _b.pixelColor(_x, _y)
                    if max(abs(_p.red() - _q.red()), abs(_p.green() - _q.green()), abs(_p.blue() - _q.blue())) > 12:
                        _n += 1
            return _n

        def _tree_case():
            # 树木遮挡（2026-11-15 新增）：从真实树盒里挑一棵，炮位放树的一侧、
            # 目标放另一侧，看判定是不是被树挡的。顺带验一条边界：
            # 树能挡弹道，但**不能**把阵地/目标抬到树冠上去（树只参与遮挡）。
            _rnd = random.Random(20261115)
            _hit = None
            for _i in range(900):
                _k = _rnd.randrange(_lm.tcount)
                _top = float(_lm.ttop[_k])
                if not (4.0 <= _top - float(_lm.tbot[_k]) <= 22.0):
                    continue
                _cx, _cy = float(_lm.tcx[_k]), float(_lm.tcy[_k])
                _a = _rnd.uniform(0.0, 6.283185307179586)
                _ux, _uz = math.cos(_a), math.sin(_a)
                _g = (_cx - _ux * 140.0, _cy - _uz * 140.0)
                _tg = (_cx + _ux * 240.0, _cy + _uz * 240.0)
                if not (0.0 < _g[0] < _md.spanx and 0.0 < _g[1] < _md.spanz):
                    continue
                if not (0.0 < _tg[0] < _md.spanx and 0.0 < _tg[1] < _md.spanz):
                    continue
                _info, _hg, _sol = _trace_at(_g, _tg)
                if _info and _info.get('veg'):
                    _hit = (_g, _tg, _info, _k)
                    break
            _sk('LOS', '真实数据里找到被树挡的射击', _hit is not None)
            if _hit is None:
                return
            _g, _tg, _info, _k = _hit
            _dtot = math.hypot(_tg[0] - _g[0], _tg[1] - _g[1])
            _hp = _info.get('hit') or (0.0, 0.0)
            _s = float(_info.get('s') or 0.0)
            _sh = math.hypot(_hp[0] - _g[0], _hp[1] - _g[1])
            print('LOS tree', _info.get('label'), 's=%.1f' % _s, 'dist=%.1f' % _dtot,
                  'top=%.1f' % float(_info.get('top') or 0.0),
                  'shadow', len(_info.get('shadow') or []), flush=True)
            _sk('LOS', '树挡的类别是树木/灌木', _info.get('label') in ('树木', '灌木'))
            _sk('LOS', '树挡挡点在连线上', abs(_sh - _s) < 1.5)
            _sk('LOS', '树挡挡在炮位和目标之间', 0.0 < _s < _dtot)
            _sk('LOS', '树挡也画被挡区域', len(_info.get('shadow') or []) == 4)
            _sk('LOS', '树挡带树冠占地四角', len(_info.get('foot') or []) == 4)
            _dz, _ = los.support_at(_md, _lm, float(_lm.tcx[_k]), float(_lm.tcy[_k]))
            _sk('LOS', '树不把阵地抬到树冠', _dz < 0.05)

        def _los_run():
            _tree_case()
            _blk = _find(True)
            _sk('LOS', '真实数据里找到被挡的射击', _blk is not None)
            _clr = _find(False)
            _sk('LOS', '真实数据里找到通畅的射击', _clr is not None)
            if _blk is None or _clr is None:
                app.quit()
                return None
            (_g, _t, _info, _hg, _sol) = _blk
            _d = math.hypot((_t[0] - _g[0]), (_t[1] - _g[1]))
            _hx, _hz = _info.get('hit') or (0.0, 0.0)
            _s = float(_info.get('s') or 0.0)
            _sh = math.hypot((_hx - _g[0]), (_hz - _g[1]))
            print('LOS blocked', _info.get('kind'), _info.get('label'), 's=%.1f' % _s,
                  'dist=%.1f' % _d, 'hit_from_gun=%.2f' % _sh,
                  'shadow', len(_info.get('shadow') or []), 'foot', bool(_info.get('foot')), flush=True)
            _sk('LOS', '挡点落在炮位→目标连线上', abs(_sh - _s) < 1.5)
            _sk('LOS', '挡点在炮位和目标之间', 0.0 < _s < _d)
            _sk('LOS', '被挡区域是四边形', len(_info.get('shadow') or []) == 4)
            _sk('LOS', '结构遮挡带地物四角', (_info.get('kind') != 'structure') or bool(_info.get('foot')))
            _sk('LOS', '挡它的东西有中文类别', bool(_info.get('label')))
            _sk('LOS', '通畅算例不报挡', not (_clr[2] or {}).get('blocked'))
            _shot['blk'] = _blk
            _shot['clr'] = _clr
            QTimer.singleShot(300, _los_ui_blocked)

        def _apply(_rec):
            _g, _t = _rec[0], _rec[1]
            win.select_weapon('ZiS3#default')
            win.set_arc('auto')
            win.gun = (float(_g[0]), float(_g[1]))
            win.view.gun = win.gun
            win.target = (float(_t[0]), float(_t[1]))
            win.view.target = win.target
            win.pend = []
            win.recompute()

        def _los_ui_blocked():
            _apply(_shot['blk'])
            _res = win.los_res or {}
            _m = _res.get('main') or {}
            print('LOS ui blocked', _m.get('blocked'), _m.get('label'), 'ms=%.1f' % float(_res.get('ms') or 0),
                  '| extra:', win.lb_extra.text(), '| status:', win.lb_status.text(), flush=True)
            _sk('LOS', 'recompute 判出被挡', bool(_m.get('blocked')))
            _sk('LOS', '状态行写了弹道被挡', '弹道被挡' in win.lb_status.text())
            _sk('LOS', '详情行写了挡点和类别', ('挡 ' in win.lb_extra.text()) and bool(_m.get('label')))
            _sk('LOS', '浮层也提示被挡', '弹道被挡' in (win.overlay.note or ''))
            _sk('LOS', '小地图描述不为空', win._los_desc() is not None)
            _sk('LOS', '判定耗时在 50 ms 内', float(_res.get('ms') or 999.0) < 50.0)
            win._open_minimap(win.target, 0.0, me=win.gun)
            QTimer.singleShot(700, _los_ui_shot)

        def _los_ui_shot():
            _mm = win.minimap
            _sk('LOS', '小地图弹出来了', _mm.isVisible())
            _sk('LOS', '小地图拿到了被挡描述', _mm.los is not None)
            _sk('LOS', '被挡时视野放大到能看见挡点', float(_mm.span) > 100.5)
            _sk('LOS', '窗口边长封顶 420', _mm.width() <= 420)
            _c0 = _los_calls[0]
            _mm.repaint()
            _sk('LOS', '小地图真的调用了遮挡绘制', _los_calls[0] > _c0)
            _mm.grab().save(str((HERE / 'shot_los_minimap.png')))
            win.grab().save(str((HERE / 'shot_los_main.png')))
            # 再截一张：把右栏滚到「诸元」组，证明面板里真的写了「弹道被挡 … 树木」
            win.scroll.ensureWidgetVisible(win.gb_res, 0, 0)
            app.processEvents()
            win.grab().save(str((HERE / 'shot_los_panel.png')))
            print('LOS panel status', win.lb_status.text(), flush=True)
            # 同一块视野做 A/B：摘掉被挡描述截一张，装回去再截一张，差集就是斜线区
            _keep = _mm.los
            _mm.set_spread(None)
            _mm.los = None
            _mm.repaint()
            _img_off = _mm.grab().toImage()
            _mm.los = _keep
            _mm.repaint()
            _img_on = _mm.grab().toImage()
            _d = _diff_px(_img_off, _img_on)
            print('LOS minimap diff px', _d, 'span', _mm.span, 'side', _mm.width(), flush=True)
            _sk('LOS', '斜线区真的画在图上了', _d > 300)
            QTimer.singleShot(400, _los_ui_clear)

        def _los_ui_clear():
            _apply(_shot['clr'])
            _res = win.los_res or {}
            _m = _res.get('main') or {}
            _sk('LOS', '通畅时判定为不挡', not _m.get('blocked'))
            _sk('LOS', '通畅时状态行不再提被挡', '弹道被挡' not in win.lb_status.text())
            _sk('LOS', '通畅时小地图描述为空', win._los_desc() is None)
            win._open_minimap(win.target, 0.0, me=win.gun)
            QTimer.singleShot(700, _los_ui_clear_shot)

        def _los_ui_clear_shot():
            _mm = win.minimap
            _sk('LOS', '通畅时小地图不画红区', _mm.los is None)
            _c1 = _los_calls[0]
            _mm.set_spread(None)
            _mm.repaint()
            print('LOS clear paint calls', _los_calls[0] - _c1, flush=True)
            _sk('LOS', '通畅时不再画遮挡层', _los_calls[0] == _c1)
            _mm.grab().save(str((HERE / 'shot_los_clear.png')))
            QTimer.singleShot(300, _dz_run)

        def _dz_run():
            # 阵地高度：桥面 / 集装箱自动抬升 + 手动覆盖（2026-11-14 用户提问后新增）。
            # 楼体 / 屋顶只给提示不自动抬 —— 盒是整栋楼的包围盒，人站一层也会落进去，
            # 自动抬到楼顶会凭空多出十几米，比不修正更糟。
            _hint_ok = False
            for _i in range(min(_lm.count, 5000) if _lm is not None else 0):
                if los.KINDS[int(_lm.kind[_i])] not in ('building', 'house', 'roof'):
                    continue
                _px, _pz = float(_lm.cx[_i]), float(_lm.cy[_i])
                if los.support_at(_md, _lm, _px, _pz)[0] < 0.05:
                    _hh, _hl = los.roof_hint(_md, _lm, _px, _pz)
                    if _hh >= 1.0:
                        print('DZ hint', _hl, round(_hh, 1), '| auto', los.support_at(_md, _lm, _px, _pz), flush=True)
                        _hint_ok = True
                        break
            _sk('LOS', '楼体只给提示、不自动抬升', _hint_ok)
            # 悬空盒不能算「脚下那块」：水塔（盒顶离地 15 m、盒底 12.5 m）、放在屋顶上的货箱，
            # 盒中心落在塔下/楼下的地面上，旧逻辑会把玩家抬到塔顶。要求盒底离地 ≤1 m。
            _float_ok = False
            for _i in range(_lm.count if _lm is not None else 0):
                if los.KINDS[int(_lm.kind[_i])] not in ('container', 'crate', 'bridge'):
                    continue
                _px, _pz = float(_lm.cx[_i]), float(_lm.cy[_i])
                _g = _md.h_at(_px, _pz)
                _top = float(_lm.top[_i]) - _g
                _bot = float(_lm.bot[_i]) - _g
                if _top >= 5.0 and _bot > 2.0:
                    _dzf, _ = los.support_at(_md, _lm, _px, _pz)
                    print('DZ float skip top %.1f bot %.1f -> %s' % (_top, _bot, _dzf), flush=True)
                    _float_ok = _dzf < 0.05
                    break
            _sk('LOS', '悬空的箱顶 / 塔顶不自动抬升', _float_ok)
            _br = None
            for _i in range(_lm.count if _lm is not None else 0):
                if los.KINDS[int(_lm.kind[_i])] in ('bridge', 'container'):
                    _br = (float(_lm.cx[_i]), float(_lm.cy[_i]))
                    break
            _sk('LOS', '数据里找到桥面 / 集装箱', _br is not None)
            if _br is None:
                _dz_finish()
                return None
            win.select_weapon('ZiS3#default')
            win.set_arc('auto')
            win.gun = _br
            win.view.gun = _br
            win.target = (min(_md.spanx - 20.0, _br[0] + 900.0), _br[1])
            win.view.target = win.target
            win.pend = []
            win.recompute()
            _gnd = _md.h_at(*_br)
            try:
                win.grab().save(str((HERE / 'shot_dz_main.png')))
                _sb0 = win.scroll.verticalScrollBar().value()
                win.scroll.ensureWidgetVisible(win.gb_res, 0, 0)
                win.panel.grab().save(str((HERE / 'shot_dz_panel.png')))
                win.scroll.verticalScrollBar().setValue(_sb0)
            except Exception:
                pass
            print('DZ auto', round(win.gun_dz, 2), win.gun_dz_label,
                  '| hgun', round(win.hgun, 2), 'gnd', round(_gnd, 2),
                  '| lb_dz', win.lb_dz.text(), flush=True)
            _sk('LOS', '阵地落在桥面 / 集装箱上自动抬升', win.gun_dz >= 0.3)
            _sk('LOS', '自动抬升带地物类别', bool(win.gun_dz_label))
            _sk('LOS', '炮位高 = 地形 + 抬升', abs(win.hgun - (_gnd + win.gun_dz)) < 0.01)
            _sk('LOS', '输入框同步了抬升值', abs(win.spin_dz.value() - round(win.gun_dz, 1)) < 0.06)
            _auto = win.gun_dz
            _hs = _md.h_at(*win.gun)
            _ht = _md.h_at(*win.target)
            _a = core.evaluate(_wz, win.gun, win.target, _hs, _ht, 'auto')
            _b = core.evaluate(_wz, win.gun, win.target, _hs + _auto, _ht, 'auto')
            _sa, _sb = _a.get('sol'), _b.get('sol')
            _sk('LOS', '抬升确实进了诸元', bool(_sa and _sb and abs(_sa['mil'] - _sb['mil']) > 0.02))
            if _sa and _sb:
                print('DZ mil ground %.2f -> lifted %.2f' % (_sa['mil'], _sb['mil']), flush=True)
            win.spin_dz.setValue(0.0)       # 手动覆盖：我就站地面上
            _sk('LOS', '手动填 0 就按地形算', abs(win.gun_dz) < 0.01 and win._gun_dz_manual == 0.0)
            _sk('LOS', '手动值进了炮位高', abs(win.hgun - _hs) < 0.01)
            print('DZ manual', win.gun_dz, '| lb_dz', win.lb_dz.text(), flush=True)
            win.gun_dz_reset()              # 「自动」按钮恢复
            _sk('LOS', '「自动」按钮恢复自动值',
                win._gun_dz_manual is None and abs(win.gun_dz - _auto) < 0.01)
            # 目标高度：把炮位摆到 900 m 外，目标放到桥面上 → 目标也该自动抬
            win.gun = (max(20.0, _br[0] - 900.0), _br[1])
            win.view.gun = win.gun
            win.target = _br
            win.view.target = _br
            win.recompute()
            _tg = _md.h_at(*_br)
            try:
                _sb1 = win.scroll.verticalScrollBar().value()
                win.scroll.ensureWidgetVisible(win.gb_res, 0, 0)
                win.panel.grab().save(str((HERE / 'shot_dz_tgt.png')))
                win.scroll.verticalScrollBar().setValue(_sb1)
            except Exception:
                pass
            print('DZ tgt', round(win.tgt_dz, 2), win.tgt_dz_label,
                  '| htg', round(win.htg, 2), 'gnd', round(_tg, 2),
                  '| lb_tdz', win.lb_tdz.text(), flush=True)
            _sk('LOS', '目标落在桥面上自动抬升', win.tgt_dz >= 0.3)
            _sk('LOS', '目标高 = 地形 + 抬升', abs(win.htg - (_tg + win.tgt_dz)) < 0.01)
            _sk('LOS', '目标输入框同步', abs(win.spin_tdz.value() - round(win.tgt_dz, 1)) < 0.06)
            _tdz = win.tgt_dz
            _hs2 = _md.h_at(*win.gun)
            _t1 = core.evaluate(_wz, win.gun, win.target, _hs2, _tg, 'auto')
            _t2 = core.evaluate(_wz, win.gun, win.target, _hs2, _tg + _tdz, 'auto')
            _s1, _s2 = _t1.get('sol'), _t2.get('sol')
            _dh1, _dh2 = (_t1.get('dh') or 0.0), (_t2.get('dh') or 0.0)
            _sk('LOS', '目标抬升进了高差', abs((_dh2 - _dh1) - _tdz) < 0.01)
            _sk('LOS', '目标抬升改了诸元', bool(_s1 and _s2 and abs(_s1['mil'] - _s2['mil']) > 0.02))
            print('DZ tgt dh %.1f -> %.1f' % (_dh1, _dh2), flush=True)
            win.spin_tdz.setValue(0.0)      # 手动：目标就在地面
            _sk('LOS', '目标手动填 0 就按地形算',
                abs(win.tgt_dz) < 0.01 and win._tgt_dz_manual == 0.0 and abs(win.htg - _tg) < 0.01)
            win.tgt_dz_reset()
            _sk('LOS', '目标「自动」按钮恢复',
                win._tgt_dz_manual is None and abs(win.tgt_dz - _tdz) < 0.01)
            _dz_finish()

        def _dz_finish():
            _sk('LOS', '全程没有异常进 excepthook', not _los_errs)
            if _los_errs:
                print(_los_errs[0][:2000], flush=True)
            print('LOS done', flush=True)
            app.quit()

        QTimer.singleShot(1200, _los_run)
    if '--flagtest' in args:
        # 旗点层回归自测。2026-11-14 把 exe 拷到 D:\Aegis 试跑时，一点「旗点」就满屏
        # TypeError: drawPixmap(int, int, NoneType)（main.py 的 _paint_flags 首帧 _flag_pm
        # 还是 None 就往下画）。这里照着真实路径走：开旗点 → 反复开关 → 改窗口尺寸，
        # 每一步都强制重绘并检查离屏缓存真的建起来了、里面真的画了东西。
        win.set_mode('arty')
        win._start_hotkeys = (lambda: None)
        win._set_digit_keys = (lambda k: None)
        win._set_char_keys = (lambda on: None)
        win._set_mapnum_keys = (lambda on: None)
        v = win.view

        def _flagtest_check(tag):
            try:
                v.repaint()          # 同步重绘，别等事件循环排队
            except Exception:
                print('FLAG', tag, 'repaint raised:', flush=True)
                traceback.print_exc()
                _sk('FLAG', tag + ' 重绘不抛异常', False)
                return False
            pm = getattr(v, '_flag_pm', None)
            if pm is None or pm.isNull():
                _sk('FLAG', tag + ' 离屏缓存已生成', False)
                return False
            dpr = max(1.0, float(v.devicePixelRatioF()))
            want = (int(v.width() * dpr), int(v.height() * dpr))
            _sk('FLAG', tag + ' 缓存尺寸', (pm.width(), pm.height()) == want)
            img = pm.toImage()
            painted = 0
            for y in range(0, img.height(), 3):
                for x in range(0, img.width(), 3):
                    if img.pixelColor(x, y).alpha() > 0:
                        painted += 1
            print('FLAG', tag, 'samples', painted, 'items', len(v.flag_items),
                  'pm', pm.width(), 'x', pm.height(), flush=True)
            _sk('FLAG', tag + ' 旗点真的画出来了', painted > 0)
            return painted > 0

        def _flagtest_step2():
            _flagtest_check('首帧')
            v.grab().save(str((HERE / 'shot_flags_selftest.png')))
            v.set_flags_on(False)
            v.set_flags_on(True)      # 再开一次：缓存又被清空，还得从头建
            print('FLAG reopen pm', getattr(v, '_flag_pm', None), flush=True)
            QTimer.singleShot(500, _flagtest_step3)

        def _flagtest_step3():
            _flagtest_check('重开')
            v.resize(max(640, v.width() - 80), max(480, v.height() - 60))
            QTimer.singleShot(500, _flagtest_step4)

        def _flagtest_step4():
            _flagtest_check('改尺寸')
            v.grab().save(str((HERE / 'shot_flags_selftest2.png')))
            print('FLAG done', flush=True)
            app.quit()

        def _flagtest_step1():
            v.set_flags_on(True)      # 就是点左上角那个「旗点」按钮
            print('FLAG on · items', len(v.flag_items), '· pm',
                  getattr(v, '_flag_pm', None), '· map',
                  (v.map.slug if v.map is not None else None), flush=True)
            QTimer.singleShot(700, _flagtest_step2)

        QTimer.singleShot(900, _flagtest_step1)
    if '--cleartest' in args:
        win.set_mode('recon')
        win._start_hotkeys = (lambda: None)
        win._set_digit_keys = (lambda k: None)
        win._set_char_keys = (lambda on: None)
        win._set_mapnum_keys = (lambda on: None)
        dbpath = HERE / 'targets_cleartest.db'
        try:
            win.target_db.close()
        except Exception:
            pass
        if dbpath.exists():
            dbpath.unlink()
        win.target_db = targetsdb.TargetDB(dbpath)
        slug = win.map.slug
        for _i in range(10):
            win.target_db.add(slug, ('A11-%d-1-1' % _i), (100.0 + _i), (200.0 + _i), 0.0, 'RGF', 'veh', ('T%d' % _i), 0, 0, '')
        cfails = []

        def _ccd(ch):
            win.on_hotkey((DIGIT_ID + int(ch)))

        def _ck(name, cond):
            if not cond:
                cfails.append(name)
                self_fail('clear:' + str(name))
            print('CLEAR', 'ok' if cond else 'FAIL', name)

        def _cst():
            return (win.recon or {}).get('state')

        def _sel():
            return list((win.recon or {}).get('sel') or [])

        def crun():
            try:
                print('CLEAR rows %d' % len(win._map_rows()))
                win.on_hotkey(12)
                _ck('menu', _cst() == 'clr_choose')
                _ccd('1')
                _ck('pick mode', _cst() == 'clr_pick')
                _ccd('1')
                _ck('pick first', len(_sel()) == 1)
                _ccd('1')
                _ck('toggle off', _sel() == [])
                _ccd('1')
                _ccd('9')
                _ccd('1')
                _ck('cross page 2', len(_sel()) == 2)
                _ccd('0')
                print('CLEAR status', win.lb_status.text())
                _ck('cleared 2', '已清除 2 个目标' in win.lb_status.text())
                _ck('db left 8', win.target_db.count(slug) == 8)
                print('CLEAR clip', QGuiApplication.clipboard().text())
                win.on_hotkey(12)
                _ccd('1')
                _ccd('0')
                _ck('empty sel warn', '还没选目标' in win.lb_status.text())
                QMessageBox.question = staticmethod((lambda *a, **k: QMessageBox.Yes))
                win.recon_clear('已取消清除')
                win.on_hotkey(12)
                _ccd('2')
                print('CLEAR status2', win.lb_status.text())
                _ck('clear all', '已清除 8 个目标' in win.lb_status.text())
                _ck('db empty', win.target_db.count(slug) == 0)
            except Exception:
                import traceback as _tb
                print('CLEAR EXCEPTION', _tb.format_exc()[:900])
                cfails.append('exception')
            print('CLEAR', (('FAILED %d: %s' % (len(cfails), ', '.join(cfails))) if cfails else 'ALL PASS'))
            try:
                win.target_db.close()
            except Exception:
                pass
            if dbpath.exists():
                dbpath.unlink()
            app.quit()

        QTimer.singleShot(1200, crun)
    if '--dronetest' in args:
        win.set_mode('arty')
        win.set_arty_sub('drone')
        # 散布椭圆要有解才画得出来：先给一个近一点的阵地（迫击炮 ~360 m 有高角解）
        win.select_weapon('Mortar#default')
        win.gun = (1616.7, 1650.0)
        win.view.gun = win.gun
        win.recompute()
        win._start_hotkeys = (lambda: None)
        win._set_digit_keys = (lambda k: None)
        win._set_char_keys = (lambda on: None)
        win._set_mapnum_keys = (lambda on: None)
        win.cfg['region'] = [10, 10, 200, 60]
        win.cfg['d_region_az_cmd'] = [10, 10, 60, 30]
        win.cfg['d_region_az_scout'] = [10, 40, 60, 30]
        win.ed_key.setText('sk-fake')
        dfails = []
        script = []
        seen = []

        def _dfake(region, kind, local=True, learn=True):
            seen.append(kind)
            if not script:
                dfails.append(('unexpected OCR kind=%s' % kind))
                return False
            ek, text = script.pop(0)
            if ek != kind:
                dfails.append(('OCR kind %s != %s' % (kind, ek)))
            QTimer.singleShot(0, (lambda t: (lambda: win.on_ocr_ok(t, 'local', 0)))(text))
            return True

        win._start_worker = _dfake

        def _dck(name, cond):
            if not cond:
                dfails.append(name)
                self_fail('drone:' + str(name))
            print('DRONE', 'ok' if cond else 'FAIL', name)

        def _dpump(ms):
            end = time.time() + ms / 1000.0
            while time.time() < end:
                app.processEvents()
                time.sleep(0.01)

        def _daz():
            v = (win.drone or {}).get('az')
            return 216.0 if v is None else float(v)

        def drun():
            try:
                win.cfg['drone_who'] = 'scout'
                win.drone = None
                win.target = None
                win._close_minimap()
                seen[:] = []
                script[:] = [('az', '216'), ('pos', 'E5-2-4')]
                win.on_hotkey(1)
                _dpump(150)
                st = (win.drone or {}).get('state')
                _dck('scout step1 az->pos', st == 'pos')
                _dck('scout az 216', abs(_daz() - 216.0) < 0.01)
                win.on_hotkey(1)
                _dpump(200)
                _dck('scout step2 done', (win.drone or {}).get('state') == 'done')
                _dck('scout minimap', win.minimap.isVisible())
                _dck('scout target', win.target is not None)
                _dck('scout ocr order', seen == ['az', 'pos'])
                print('DRONE scout log', seen, '| target', win.target)
                # 散布椭圆：小地图上要按当前解画出来（横向 sprA、纵向 sprB）
                _sp = win.minimap.spread or {}
                _dck('scout spread nonzero', float(_sp.get('cross') or 0) > 0 and float(_sp.get('along') or 0) > 0)
                win.minimap.grab().save(str((HERE / 'shot_minimap_spread.png')))
                print('DRONE spread', _sp)
                # 真点一下：点完停 1 秒（这 1 秒里散布要跟着新解更新），然后自动收起。
                # 不用 QtTest（打包时 --exclude-module PySide6.QtTest 把它踢了，exe 里会 ImportError），
                # 直接造一个鼠标按下事件发给小地图，走的是同一条 mousePressEvent。
                _pt = win.minimap.w2s(*win.target)
                _ev = QMouseEvent(QEvent.MouseButtonPress, QPointF(_pt.x(), _pt.y()),
                                  Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
                QApplication.sendEvent(win.minimap, _ev)
                _dpump(250)
                _dck('scout linger visible', win.minimap.isVisible())
                _dck('scout spread after pick', bool(win.minimap.spread))
                win.minimap.grab().save(str((HERE / 'shot_minimap_spread_mark.png')))
                _dpump(1000)
                _dck('scout autoclose 1s', not win.minimap.isVisible())
                # 第二轮：小地图收掉后再按热键，必须重新读方位角（不能沿用上一轮的角度）
                script[:] = [('az', '090'), ('pos', 'E5-3-3')]
                seen[:] = []
                win.on_hotkey(1)
                _dpump(150)
                _dck('scout cycle2 az first', (win.drone or {}).get('state') == 'pos' and abs(_daz() - 90.0) < 0.01)
                win.on_hotkey(1)
                _dpump(200)
                _dck('scout cycle2 done', (win.drone or {}).get('state') == 'done')
                _dck('scout cycle2 order', seen == ['az', 'pos'])
                print('DRONE scout cycle2 log', seen)
                win.cfg['drone_who'] = 'cmd'
                win.drone = None
                win.target = None
                win._close_minimap()
                seen[:] = []
                script[:] = [('pos', 'E5-2-4'), ('az', '216')]
                win.on_hotkey(1)
                _dpump(250)
                _dck('cmd one-key done', (win.drone or {}).get('state') == 'done')
                _dck('cmd ocr order pos->az', seen == ['pos', 'az'])
                _dck('cmd minimap', win.minimap.isVisible())
                print('DRONE cmd log', seen, '| target', win.target)
            except Exception:
                import traceback as _tb
                print('DRONE EXCEPTION', _tb.format_exc()[:900])
                dfails.append('exception')
            print('DRONE', (('FAILED %d: %s' % (len(dfails), ', '.join(map(str, dfails)))) if dfails else 'ALL PASS'))
            app.quit()

        QTimer.singleShot(1200, drun)
    if '--cmdtest' in args:
        win._start_hotkeys = (lambda: None)
        win._set_digit_keys = (lambda k: None)
        win._set_char_keys = (lambda on: None)
        win._set_mapnum_keys = (lambda on: None)
        win.cmd_faction = 'USA'
        win.cmd_unit = 'USA_LO_CombinedArms'
        win._refresh_cmd_combos()
        cphase = {'p': 'start', 'seen': [], 'checks': {}}
        def _cd(d):
            win.on_hotkey((DIGIT_ID + int(d)))

        def _cl(ch):
            win.on_hotkey(((LETTER_ID + ord(ch)) - 65))

        def _csnap(tag):
            c = win.cmd or {}
            cphase['seen'].append(tag)
            if c.get('build_item'):
                pass
            else:
                pass
            print('CMD', tag, 'state', c.get('state'), 'leader', c.get('leader'), 'grid', c.get('grid'), 'task', c.get('task'), 'cat', c.get('build_cat'), 'item', (c.get('build_item') or {}).get('cn'), None, 'att', c.get('attitude'))

        def ctick():
            st = (win.cmd or {}).get('state')
            p = cphase['p']
            if p == 'start':
                win.on_hotkey(31)
                cphase['p'] = 'leader'
            elif p == 'leader' and st == 'leader':
                win.on_hotkey(31)
                cphase['checks']['empty_confirm'] = ((win.cmd or {}).get('state'), win.lb_cmd_step.text())
                _cd('2')
                win.on_hotkey(BACK_ID)
                cphase['checks']['after_bs1'] = (win.cmd or {}).get('leader')
                win.on_hotkey(BACK_ID)
                _cd('2')
                _csnap('leader')
                win.on_hotkey(31)
                cphase['p'] = 'grid'
            elif p == 'grid' and st == 'grid':
                for ch in 'G0753':
                    if ch.isalpha():
                        _cl(ch)
                    else:
                        _cd(ch)
                        continue
                win.on_hotkey(31)
                cphase['checks']['short_confirm'] = ((win.cmd or {}).get('state'), win.lb_cmd_step.text())
                _cd('6')
                _csnap('grid')
                win.on_hotkey(31)
                cphase['p'] = 'task'
            elif p == 'task' and st == 'task':
                _cd('5')
                win.on_hotkey(31)
                cphase['p'] = 'cat'
            elif p == 'cat' and st == 'build_cat':
                _cd('2')
                win.on_hotkey(31)
                cphase['p'] = 'item'
            elif p == 'item' and st == 'build_item':
                _cd('1')
                _csnap('item')
                win.on_hotkey(31)
                cphase['p'] = 'att'
            elif p == 'att' and st == 'attitude':
                _cd('1')
                _csnap('att')
                win.on_hotkey(31)
                cphase['checks']['first_sentence'] = win.cmd_last
                win.on_hotkey(31)
                cphase['p'] = 'cancel_start'
            elif p == 'cancel_start' and st == 'leader':
                _cd('3')
                win.on_hotkey(31)
                win.on_hotkey(32)
                cphase['checks']['cancel'] = (win.cmd is None, win.lb_cmd_step.text())
                win.on_hotkey(31)
                cphase['p'] = 'p2_leader'
            elif p == 'p2_leader' and st == 'leader':
                _cd('1')
                win.on_hotkey(31)
                cphase['p'] = 'p2_grid'
            elif p == 'p2_grid' and st == 'grid':
                for ch in 'G07536':
                    if ch.isalpha():
                        _cl(ch)
                    else:
                        _cd(ch)
                        continue
                win.on_hotkey(31)
                cphase['p'] = 'p2_task'
            elif p == 'p2_task' and st == 'task':
                _cd('1')
                win.on_hotkey(31)
                cphase['checks']['p2_skipped_build'] = ((win.cmd or {}).get('state'),)
                cphase['p'] = 'p2_att'
            elif p == 'p2_att' and st == 'attitude':
                _cd('2')
                win.on_hotkey(31)
                cphase['checks']['second_sentence'] = win.cmd_last
                cphase['p'] = 'report'
            elif p == 'report':
                def _ov(tag):
                    return (tag, [r[0] for r in win.overlay.rows], win.overlay.status_text, list((win.overlay.para or [])))

                win.set_mode('arty')
                cphase['checks']['overlay_arty'] = _ov('arty')
                win.set_mode('recon')
                cphase['checks']['overlay_recon'] = _ov('recon')
                win.set_mode('command')
                cphase['checks']['overlay_cmd'] = _ov('command')
                win.panel.grab().save(str((HERE / 'shot_cmd_panel.png')))
                win.overlay.grab().save(str((HERE / 'shot_cmd_overlay.png')))
                import json as _json
                report = {'sentence': cphase['checks'].get('first_sentence'), 'second': cphase['checks'].get('second_sentence'), 'copy_ok': getattr(win, 'cmd_copy_ok', None), 'step': win.lb_cmd_step.text(), 'panel_text': win.lb_cmd_text.text(), 'para': win.overlay.para, 'overlay_size': [win.overlay.width(), win.overlay.height()], 'unit_label': win.lb_cunit.text(), 'mode_checked': win.btn_mode_cmd.isChecked(), 'gb_cmd_visible': win.gb_cmd.isVisible(), 'checks': cphase['checks'], 'seen': cphase['seen']}
                (HERE / 'cmdtest_result.json').write_text(_json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')
                print('CMD sentence', win.cmd_last)
                print('CMD clip', QGuiApplication.clipboard().text())
                _sk('CMD', 'sentence 1队长观察任务',
                    (win.cmd_last or '').strip() == '1队长，你去G07-5-3-6执行观察任务，完成手里的事情再执行')
                _sk('CMD', 'copied', bool(getattr(win, 'cmd_copy_ok', False)))
                app.quit()
                return None
            QTimer.singleShot(300, ctick)

        QTimer.singleShot(900, ctick)
    if '--cmdrealtest' in args:
        import ctypes as _ct
        import json as _json
        win.cmd_faction = 'USA'
        win.cmd_unit = 'USA_LO_CombinedArms'
        win._refresh_cmd_combos()
        reg_fail = []
        _orig_start = win._start_hotkeys
        def _start_real():
            _orig_start()
            try:
                win.hotkeys.failed.connect((lambda hid, err, mods: reg_fail.append([int(hid), int(err)])))
            except Exception:
                return None

        win._start_hotkeys = _start_real
        win._start_hotkeys()
        _u32 = _ct.WinDLL('user32', use_last_error=True)
        R = {'checks': {}, 'posted': [], 'missing': [], 'reg_fail': reg_fail, 'sentence': '', 'step': '', 'ok': False}
        def _thread_id():
            for _ in range(60):
                t = int((getattr(win.hotkeys, '_tid', 0) or 0))
                if t:
                    return t
                app.processEvents()
                _ct.windll.kernel32.Sleep(20)
            return 0

        def _post(hid):
            t = _thread_id()
            R['posted'].append(int(hid))
            if (not t or not _u32.PostThreadMessageW(t, 786, int(hid), 0)):
                R['missing'].append(int(hid))
                return None

        def _reg(hid):
            return any((int(b[0]) == int(hid) for b in getattr(win.hotkeys, 'bindings', [])))

        def _key31():
            return int((getattr(win, '_hid_combo', {}) or {}).get(31, 0))

        def _st():
            return (win.cmd or {}).get('state')

        def _step_pos():
            for f in (206, 100, 107, 105, 103, 106):
                _post(f)

        steps = []
        def step(name, fn, check):
            steps.append((name, fn, check))

        step('start', (lambda: _post(_key31())), (lambda: ((_st() == 'leader' and win._digit_keys == tuple('0123456789') and win._char_keys == () and win.hotkeys.isRunning() and _reg(102) and _reg(400)), f"state={_st()} digit={win._digit_keys} char={len(win._char_keys)} run={win.hotkeys.isRunning()} reg2={_reg(102)} regback={_reg(400)}")))
        step('key_warn', (lambda: None), (lambda: (True, f"visible={(getattr(win, 'lb_cmd_warn', None) is not None and win.lb_cmd_warn.isVisible())} text={(getattr(win, 'lb_cmd_warn', None) and win.lb_cmd_warn.text())!r}")))
        step('leader1', (lambda: _post(101)), (lambda: ((_st() == 'leader' and (win.cmd or {}).get('leader') == '1'), f"state={_st()} leader={(win.cmd or {}).get('leader')}")))
        def _back_twice():
            _post(400)
            _post(400)

        step('back_clear', _back_twice, (lambda: ((_st() == 'leader' and (win.cmd or {}).get('leader') == ''), f"state={_st()} leader={(win.cmd or {}).get('leader')!r}")))
        step('back_empty', (lambda: _post(400)), (lambda: ((_st() == 'leader' and (win.cmd or {}).get('leader') == ''), f"state={_st()} leader={(win.cmd or {}).get('leader')!r}")))
        step('leader2', (lambda: _post(102)), (lambda: ((win.cmd or {}).get('leader') == '2', f"leader={(win.cmd or {}).get('leader')}")))
        step('confirm_leader', (lambda: _post(_key31())), (lambda: ((_st() == 'grid' and len(win._char_keys) == 36 and _reg(206) and _reg(100) and _reg(400)), f"state={_st()} char={len(win._char_keys)} regG={_reg(206)} reg0={_reg(100)}")))
        step('type_grid', _step_pos, (lambda: ((win.cmd or {}).get('grid') == 'G07536', f"grid={(win.cmd or {}).get('grid')}")))
        step('confirm_grid', (lambda: _post(_key31())), (lambda: ((_st() == 'task' and (win.cmd or {}).get('grid') == 'G07-5-3-6' and win._char_keys == () and (not _reg(206)) and _reg(105)), f"state={_st()} grid={(win.cmd or {}).get('grid')} char={len(win._char_keys)} regG={_reg(206)} reg5={_reg(105)}")))
        step('task_bad', (lambda: _post(109)), (lambda: ((_st() == 'task' and '1–8' in win.lb_cmd_step.text() and (not (win.cmd or {}).get('task'))), f"state={_st()} step={win.lb_cmd_step.text()!r}")))
        step('task_build', (lambda: _post(105)), (lambda: ((win.cmd or {}).get('task') == '建造', f"task={(win.cmd or {}).get('task')}")))
        step('confirm_task', (lambda: _post(_key31())), (lambda: ((_st() == 'build_cat' and (not (win.cmd or {}).get('build_cat'))), f"state={_st()} cat={(win.cmd or {}).get('build_cat')!r}")))
        step('cat_bad', (lambda: _post(105)), (lambda: ((_st() == 'build_cat' and (not (win.cmd or {}).get('build_cat')) and '1–3' in win.lb_cmd_step.text()), f"state={_st()} step={win.lb_cmd_step.text()!r}")))
        step('cat2', (lambda: _post(102)), (lambda: ((win.cmd or {}).get('build_cat') == 'fortifications', f"cat={(win.cmd or {}).get('build_cat')}")))
        step('confirm_cat', (lambda: _post(_key31())), (lambda: ((_st() == 'build_item' and int(((win.cmd or {}).get('page') or 0)) == 0 and len(win.overlay.options) == 8), f"state={_st()} page={(win.cmd or {}).get('page')} opts={len(win.overlay.options)}")))
        step('page_next', (lambda: _post(109)), (lambda: ((_st() == 'build_item' and int(((win.cmd or {}).get('page') or 0)) == 1 and len(win.overlay.options) == 2), f"state={_st()} page={(win.cmd or {}).get('page')} opts={len(win.overlay.options)}")))
        step('page_end', (lambda: _post(109)), (lambda: ((_st() == 'build_item' and int(((win.cmd or {}).get('page') or 0)) == 1 and '最后一页' in win.lb_cmd_step.text()), f"page={(win.cmd or {}).get('page')} step={win.lb_cmd_step.text()!r}")))
        step('item1', (lambda: _post(101)), (lambda: (bool((win.cmd or {}).get('build_item')), f"item={((win.cmd or {}).get('build_item') or {}).get('cn')}")))
        step('confirm_item', (lambda: _post(_key31())), (lambda: (_st() == 'attitude', f"state={_st()}")))
        step('att2', (lambda: _post(102)), (lambda: ((win.cmd or {}).get('attitude') == '完成手里的事情再执行', f"att={(win.cmd or {}).get('attitude')}")))
        step('finish', (lambda: _post(_key31())), (lambda: (((not _reg(400)) and win.overlay.para is not None and win.overlay.para[0] == '命令' and win.overlay.status_text == '命令已复制' and win.overlay.status_state == 'done' and getattr(win, 'cmd_copy_ok', None) is True and str(getattr(win, 'cmd_last', '')).startswith('2队长，你去G07-5-3-6执行建造') and str(getattr(win, 'cmd_last', '')).endswith('任务，完成手里的事情再执行')), f"cmd={win.cmd} keys={win._digit_keys}/{len(win._char_keys)} back={_reg(400)} para={win.overlay.para} status={win.overlay.status_text!r}/{win.overlay.status_state} copy={getattr(win, 'cmd_copy_ok', None)} last={getattr(win, 'cmd_last', '')!r}")))
        step('start2', (lambda: _post(_key31())), (lambda: (_st() == 'leader', f"state={_st()}")))
        def _leader_then_confirm():
            _post(103)
            _post(_key31())

        step('leader3', _leader_then_confirm, (lambda: ((_st() == 'grid' and (win.cmd or {}).get('leader') == '3'), f"state={_st()} leader={(win.cmd or {}).get('leader')}")))
        def _grid3():
            _post(206)
            _post(100)
            _post(107)

        step('grid3', _grid3, (lambda: ((win.cmd or {}).get('grid') == 'G07', f"grid={(win.cmd or {}).get('grid')}")))
        step('drop_mode', (lambda: win.set_mode('arty')), (lambda: ((win.cmd is None and win.mode == 'arty' and win._digit_keys == () and win._char_keys == () and (not _reg(400)) and (not win.gb_cmd.isVisible()) and win.gb_res.isVisible() and win.overlay.options == []), f"cmd={win.cmd} mode={win.mode} keys={win._digit_keys}/{len(win._char_keys)} back={_reg(400)} gbcmd={win.gb_cmd.isVisible()} opts={len(win.overlay.options)}")))
        step('back_cmd', (lambda: win.set_mode('command')), (lambda: ((win.mode == 'command' and win.gb_cmd.isVisible() and bool(win.lb_cmd_step.text())), f"mode={win.mode} gbcmd={win.gb_cmd.isVisible()} step={win.lb_cmd_step.text()!r}")))
        def _no_unit():
            win.cmd_unit = ''
            _post(_key31())

        step('no_unit', _no_unit, (lambda: ((win.cmd is None and '先在面板里选我方阵营' in win.lb_cmd_step.text() and win.overlay.rows and win.overlay.rows[0] == ('我方', '未选我方部队') and win.overlay.status_state == 'error'), f"cmd={win.cmd} step={win.lb_cmd_step.text()!r} rows={win.overlay.rows} status={win.overlay.status_state}")))
        step('restore_unit', (lambda: setattr(win, 'cmd_unit', 'USA_LO_CombinedArms')), (lambda: (win._cmd_unit() is not None, f"unit={win.cmd_unit}")))
        idx = {'i': 0, 'wait': None}
        guard = {'done': False}
        def finish_rt():
            if guard['done']:
                return None
            guard['done'] = True
            R['sentence'] = str(getattr(win, 'cmd_last', ''))
            R['step'] = win.lb_cmd_step.text()
            R['ok'] = all((bool(v[0]) for v in R['checks'].values()))
            (HERE / 'cmdrealtest_result.json').write_text(_json.dumps(R, ensure_ascii=False, indent=1), encoding='utf-8')
            for k, v in R['checks'].items():
                # 这里以前无论通过与否都打印 "RT PASS FAIL <名字>"，if/else 两支都是 pass ——
                # 既看不出哪一条挂了，也不可能让退出码变化。现在按结果打印并登记。
                _okk = bool(v[0])
                print('RT', 'ok' if _okk else 'FAIL', k, '|', v[1], flush=True)
                if not _okk:
                    self_fail('rt:' + str(k))
            print('RT sentence', R['sentence'], flush=True)
            print('RT reg_fail', reg_fail, flush=True)
            print('RT missing', R['missing'], flush=True)
            print('RT ok', R['ok'], flush=True)
            _sk('RT', 'all checks', bool(R['ok']))
            _sk('RT', 'no reg_fail', not reg_fail)
            _sk('RT', 'no missing posts', not R['missing'])
            app.quit()

        def rtick():
            w = idx['wait']
            if w:
                name, chk = w
                idx['wait'] = None
                try:
                    ok, detail = chk()
                except Exception as e:
                    detail, ok = f"CHECK EXC {e}"
                R['checks'][name] = [bool(ok), str(detail)]
                print('RT', 'PASS' if ok else 'FAIL', name, '|', detail, flush=True)
            if idx['i'] >= len(steps):
                finish_rt()
                return None
            name, fn, chk = steps[idx['i']]
            idx['i'] += 1
            try:
                fn()
            except Exception as e:
                R['checks'][name] = [False, f"EXC {e}"]
                print('RT FAIL', name, 'EXC', e, flush=True)
                finish_rt()
                return None
            else:
                idx['wait'] = (name, chk)
                QTimer.singleShot(350, rtick)

        QTimer.singleShot(900, rtick)
    if '--locatetest' in args:
        win.select_map('yehorivka')
        seen = {'warn': '', 'prefill': '', 'warned': 0}
        def _dlg():
            return QApplication.activeModalWidget()

        def _l1():
            d = _dlg()
            if isinstance(d, QInputDialog):
                d.setTextValue('Z99-9-9')
                d.accept()
                return None

        def _l2():
            d = _dlg()
            if isinstance(d, QMessageBox):
                seen['warn'] = d.text().replace("""
""", ' / ')
                seen['warned'] += 1
                d.accept()
                return None

        def _l3():
            d = _dlg()
            if isinstance(d, QInputDialog):
                seen['prefill'] = d.textValue()
                d.setTextValue('B11-2-5-6')
                d.accept()
                return None

        def _lreport():
            print('LOCATE warn', seen['warned'], '|', seen['warn'])
            print('LOCATE prefill', seen['prefill'], '| pin', win.view.pin, '| pintext', win.view.pin_text, '| status', win.lb_status.text())
            _sk('LOCATE', 'warned once', seen['warned'] == 1)
            _sk('LOCATE', 'warn says 不在这张地图上', '不在这张地图上' in (seen['warn'] or ''))
            # 弹窗是循环的：输了打不开的格号后，输入框里留着的就是刚才那段文字（这是设计，不是 bug）
            _sk('LOCATE', 'prefill keeps bad text', (seen['prefill'] or '').strip() == 'Z99-9-9')
            _sk('LOCATE', 'good grid pins', str(win.view.pin_text or '').startswith('B11-2-5-6'))
            app.quit()

        QTimer.singleShot(700, win.locate_dialog)
        QTimer.singleShot(1100, _l1)
        QTimer.singleShot(1800, _l2)
        QTimer.singleShot(2500, _l3)
        QTimer.singleShot(3300, _lreport)
    if '--shotdesk' in args:
        gun = (3083.3, 3183.3)
        win.gun = gun
        win.view.gun = gun
        tgt = win.map.grid_at((gun[0] + 430), (gun[1] + 210))
        win.set_target_grid(tgt)
        win.recompute()
        win.overlay.move(760, 300)
        def desk():
            vision.ImageGrab.grab(all_screens=True).save(str((HERE / 'shot_desktop.png')))
            print('desktop shot', tgt)
            app.quit()

        QTimer.singleShot(1600, desk)
    rc = app.exec()
    if SELF_TEST:
        # 自测退场：不等后台线程收尾就销毁 QThread 会直接崩（--locatetest 以前就是这么挂的）。
        try:
            if win.hotkeys is not None:
                win.hotkeys.stop()
                win.hotkeys.wait(800)
        except Exception:
            pass
        try:
            if win.worker and win.worker.isRunning():
                win.worker.wait(2000)
        except Exception:
            pass
        try:
            win.view.wait_decodes(3000)
        except Exception:
            pass
        try:
            win._timer.stop()
            win._timer_clear.stop()
        except Exception:
            pass
    if SELF_FAILS:
        # 自测有失败就要让调用方（人、脚本、CI）看得到：打印汇总并以 1 退场。
        print('SELFTEST FAILED %d: %s' % (len(SELF_FAILS), self_fail_text()), flush=True)
        return 1
    if SELF_TEST:
        print('SELFTEST ALL PASS', flush=True)
    return rc

if __name__ == "__main__":
    sys.exit(main())
