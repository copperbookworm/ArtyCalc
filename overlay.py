# -*- coding: utf-8 -*-
"""半透明浮窗：底半透明、文字不透明，显示状态 + 4 个值。"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QMenu, QWidget

BG = QColor(9, 13, 19, 165)
BORDER = QColor(255, 178, 90, 200)
LABEL = QColor(160, 178, 196, 255)
VALUE = QColor(255, 255, 255, 255)
ACCENT = QColor(255, 209, 102, 255)
ERROR = QColor(255, 130, 130, 255)
NOTE = QColor(150, 166, 182, 255)

STATUS_COLOR = {
    "idle": QColor(150, 166, 182, 255),
    "busy": QColor(255, 209, 102, 255),
    "done": QColor(126, 227, 139, 255),
    "error": QColor(255, 130, 130, 255),
}
STATUS_TEXT = {
    "idle": "待命",
    "busy": "参数计算中",
    "done": "计算完成",
    "error": "计算失败",
}


class Overlay(QWidget):
    moved_by_user = Signal(int, int)
    show_main = Signal()          # 双击浮窗 / 右键菜单「显示主界面」

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.Tool
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setWindowTitle("间瞄诸元")
        # 角度那一行的标签：迫击炮是「密位」，有射角的武器（火箭巢 / 车载炮 /
        # 榴弹发射器）是「射角」——由主程序按当前武器调用 set_angle_label()
        self.angle_label = "密位"
        # 顺序按需求：角度在方位上面，方位是最底下一行
        self.rows = [
            ("阵地", "—"),
            ("目标", "—"),
            (self.angle_label, "—"),
            ("方位", "—"),
        ]
        self.note = "按 F1 识别目标坐标"
        # 选项列表（侦察：选敌方目标类型 / 选要报告的目标）——数字键选择
        self.options = []          # [(键, 文案), ...]
        self.options_title = ""
        self.options_footer = ""
        self.para = None           # (标题, 整段文字)：发布命令时显示整句话（自动换行）
        self.bad = False
        self.status_state = "idle"
        self.status_text = STATUS_TEXT["idle"]
        self._dots = 0
        # 计时那一行（浮窗最上面一行）：text 为空就不画；run=计时中 / stop=刚结束
        self.timer_text = ""
        self.timer_state = "run"
        # 多行诸元块（呼叫弹幕每轮每门一行）：None=不显示；(标题, [(编号, 文字, ok), ...], 脚注)
        # 有它时窗口加宽到 _w_wide，免得每门的「密位 / 方位 / 距离」被省略号切掉
        self.sol = None
        self._drag = None
        self._w = 344
        self._w_wide = 430          # 有诸元块时的宽度（实字体下「密位 1578.2 mil · 方位 55.5° · 53 m」约 283 px，放得下）
        self._pulse = QTimer(self)
        self._pulse.setInterval(320)
        self._pulse.timeout.connect(self._tick)
        self.resize(self._w, self.height_for())

    # ---------- 内容 ----------
    def set_rows(self, rows, note: str = "", bad: bool = False, sol=None):
        """任意行数（侦察报告要 6 行）。rows = [(标签, 值), ...]。

        sol：可选的多行诸元块（呼叫弹幕每轮每门一行），格式
        (标题, [(编号, 文字, 是否正常), ...], 脚注)；传 None 就把这块收掉。
        """
        self.para = None
        self.rows = [(str(a), str(b)) for a, b in rows]
        self.note = note
        self.bad = bad
        self._set_sol(sol)
        self.resize(self._w, self.height_for())
        self._fit_screen()
        self.update()

    def _set_sol(self, sol):
        self.sol = sol or None
        self._w = self._w_wide if self.sol else 344

    def set_angle_label(self, label: str):
        """角度那一行的标签跟着武器走：「密位」或「射角」。
        只改 4 行间瞄布局里的那一格，侦察报告的 6 行布局不动。"""
        label = str(label or "密位")
        old = self.angle_label
        if label == old:
            return
        self.angle_label = label
        if len(self.rows) == 4 and self.rows[2][0] == old:
            self.rows[2] = (label, self.rows[2][1])
            self.update()

    def set_values(self, gun: str, target: str, bearing: str, mil: str, note: str = "", bad: bool = False):
        # 注意参数顺序未变（gun, target, bearing, mil），只是摆出来的行序改成
        # 阵地 / 目标 / 角度 / 方位——方位固定在最下面一行；
        # 角度那行的标签由 set_angle_label() 决定（密位 / 射角）
        self.set_rows([("阵地", gun), ("目标", target), (self.angle_label, mil),
                       ("方位", bearing)], note, bad)

    def set_options(self, title: str, items, footer: str = ""):
        """浮窗里列一组编号选项（1–9），玩家按小键盘数字选。items=[(键, 文案), ...]。"""
        self.para = None
        self.options = [(str(k), str(v)) for k, v in (items or [])]
        self.options_title = title or ""
        self.options_footer = footer or ""
        self.resize(self._w, self.height_for())
        self.update()

    def clear_options(self):
        """收起选项列表（不动上面的行）。"""
        if not self.options and not self.options_title:
            return
        self.options = []
        self.options_title = ""
        self.options_footer = ""
        self.resize(self._w, self.height_for())
        self.update()

    def set_para(self, title: str, text: str, note: str = "", state: str = "done",
                 status_text: str = ""):
        """整段文字（发布命令的整句）：自动换行显示。状态行用 status_text（留空则用 title）。"""
        self.rows = []
        self.options = []
        self.options_title = ""
        self.options_footer = ""
        self.para = (str(title or ""), str(text or ""))
        self.note = note
        self.bad = False
        self._set_sol(None)
        self.set_status(state, status_text or title)
        self.resize(self._w, self.height_for())
        self.update()

    @staticmethod
    def _para_font():
        f = QFont("Microsoft YaHei UI", 11)
        f.setBold(True)
        return f

    def _para_height(self) -> int:
        if not self.para:
            return 0
        fm = QFontMetrics(self._para_font())
        return fm.boundingRect(0, 0, self._w - 34, 10000, Qt.TextWordWrap,
                               self.para[1]).height()

    def set_note(self, note: str):
        """只换底部提示，不动行与行数（侦察模式是 6 行，不能被改成 4 行）。"""
        self.note = note
        self.resize(self._w, self.height_for())
        self.update()

    def set_status(self, state: str = "idle", text: str = ""):
        """state: idle / busy / done / error。文字留空则用默认文案。"""
        self.status_state = state or "idle"
        self.status_text = text or STATUS_TEXT.get(self.status_state, STATUS_TEXT["idle"])
        if self.status_state == "busy":
            self._dots = 1
            self._pulse.start()
        else:
            self._pulse.stop()
            self._dots = 0
        self.resize(self._w, self.height_for())
        self.update()

    def set_timer(self, text: str, state: str = "run"):
        """浮窗第一行的计时：text 为空则把这一行收起来（高度跟着变）。

        state：run=计时中（橙色）/ stop=刚结束（绿色，主窗口过几秒会清掉）。
        """
        text = str(text or "")
        state = state or "run"
        if text == self.timer_text and state == self.timer_state:
            return
        self.timer_text = text
        self.timer_state = state
        self.resize(self._w, self.height_for())
        self.update()

    def _tick(self):
        self._dots = (self._dots + 1) % 4
        self.update()

    def height_for(self) -> int:
        extra = 32 if self.timer_text else 0
        if self.para:
            h = 12 + 26 + 8 + (20 if self.para[0] else 0) + self._para_height()
            h += (24 if self.note else 8)
            return h + extra
        h = 12 + 26 + len(self.rows) * 34 + (24 if self.note else 8)
        if self.sol:
            h += 20 + 20 * len(self.sol[1]) + (18 if (len(self.sol) > 2 and self.sol[2]) else 2)
        if self.options:
            h += 20 + 24 * len(self.options) + (20 if self.options_footer else 4)
        return h + extra

    # ---------- 绘制 ----------
    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(1, 1, self.width() - 2, self.height() - 2)
        path = QPainterPath()
        path.addRoundedRect(rect, 13, 13)

        p.fillPath(path, BG)
        pen = QPen(BORDER, 2)
        p.setPen(pen)
        p.drawPath(path)

        # 左侧橙色装饰条
        p.setPen(Qt.NoPen)
        p.setBrush(ACCENT)
        p.drawRoundedRect(QRectF(1, 14, 4, self.height() - 28), 2, 2)

        label_font = QFont("Microsoft YaHei UI", 10)
        value_font = QFont("Consolas", 15)
        value_font.setBold(True)

        # —— 计时行（浮窗第一行）：只在计时中 / 刚结束时出现
        y = 10
        if self.timer_text:
            p.setFont(QFont("Microsoft YaHei UI", 9))
            p.setPen(LABEL)
            p.drawText(QRectF(18, y, 52, 26), Qt.AlignLeft | Qt.AlignVCenter,
                       "计时" if self.timer_state == "run" else "已结束")
            tv_font = QFont("Consolas", 17)
            tv_font.setBold(True)
            p.setFont(tv_font)
            p.setPen(ACCENT if self.timer_state == "run" else QColor(126, 227, 139))
            p.drawText(QRectF(70, y, self.width() - 88, 26), Qt.AlignLeft | Qt.AlignVCenter,
                       self.timer_text)
            y += 32

        # —— 状态行
        color = STATUS_COLOR.get(self.status_state, STATUS_COLOR["idle"])
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawEllipse(QPoint(25, y + 13), 4, 4)
        p.setBrush(Qt.NoBrush)
        text = self.status_text
        if self.status_state == "busy":
            text = self.status_text + "." * self._dots
        st_font = QFont("Microsoft YaHei UI", 10)
        st_font.setBold(True)
        p.setFont(st_font)
        p.setPen(color)
        text = QFontMetrics(st_font).elidedText(text, Qt.ElideRight, int(self.width() - 54))
        p.drawText(QRectF(36, y, self.width() - 52, 26), Qt.AlignLeft | Qt.AlignVCenter, text)
        y += 26

        if self.para:
            pt, ptext = self.para
            if pt:
                tf = QFont("Microsoft YaHei UI", 9)
                tf.setBold(True)
                p.setFont(tf)
                p.setPen(ACCENT)
                p.drawText(QRectF(18, y, self.width() - 34, 18),
                           Qt.AlignLeft | Qt.AlignVCenter, pt)
                y += 20
            pf = self._para_font()
            p.setFont(pf)
            p.setPen(VALUE)
            hh = self._para_height()
            p.drawText(QRectF(18, y, self.width() - 34, hh),
                       Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignTop, ptext)
            y += hh + 4

        for label, value in self.rows:
            p.setFont(label_font)
            p.setPen(LABEL)
            p.drawText(QRectF(18, y, 52, 30), Qt.AlignLeft | Qt.AlignVCenter, label)
            p.setFont(value_font)
            p.setPen(ERROR if (self.bad and value in ("—", "无坐标")) else VALUE)
            value = QFontMetrics(value_font).elidedText(value, Qt.ElideRight, int(self.width() - 92))
            p.drawText(QRectF(70, y, self.width() - 88, 30), Qt.AlignLeft | Qt.AlignVCenter, value)
            y += 34

        # —— 多行诸元块（呼叫弹幕）：标题 + 每门一行「编号 + 角度 / 方位 / 距离」
        if self.sol:
            s_title, s_items = self.sol[0], self.sol[1]
            s_foot = self.sol[2] if len(self.sol) > 2 else ""
            stf = QFont("Microsoft YaHei UI", 9)
            stf.setBold(True)
            p.setFont(stf)
            p.setPen(ACCENT)
            p.drawText(QRectF(18, y, self.width() - 34, 20),
                       Qt.AlignLeft | Qt.AlignVCenter, s_title)
            y += 20
            uid_font = QFont("Consolas", 11)
            uid_font.setBold(True)
            val_font = QFont("Consolas", 11)
            val_font.setBold(True)
            for item in s_items:
                uid, val = item[0], item[1]
                ok = item[2] if len(item) > 2 else True
                p.setFont(uid_font)
                p.setPen(ACCENT)
                p.drawText(QRectF(18, y, 40, 20), Qt.AlignLeft | Qt.AlignVCenter, uid)
                p.setFont(val_font)
                p.setPen(ERROR if not ok else VALUE)
                val = QFontMetrics(val_font).elidedText(val, Qt.ElideRight,
                                                        int(self.width() - 62))
                p.drawText(QRectF(58, y, self.width() - 76, 20),
                           Qt.AlignLeft | Qt.AlignVCenter, val)
                y += 20
            if s_foot:
                ff = QFont("Microsoft YaHei UI", 9)
                p.setFont(ff)
                p.setPen(NOTE)
                foot = QFontMetrics(ff).elidedText(s_foot, Qt.ElideRight, int(self.width() - 36))
                p.drawText(QRectF(18, y - 2, self.width() - 34, 18),
                           Qt.AlignLeft | Qt.AlignVCenter, foot)
                y += 18           # 脚注占掉这一行，别和下面的提示行叠在一起
            else:
                y += 2

        if self.note:
            nt_font = QFont("Microsoft YaHei UI", 9)
            p.setFont(nt_font)
            p.setPen(ERROR if self.bad else NOTE)
            note = QFontMetrics(nt_font).elidedText(self.note, Qt.ElideRight, int(self.width() - 36))
            p.drawText(QRectF(18, y - 4, self.width() - 34, 20), Qt.AlignLeft | Qt.AlignVCenter, note)

        # —— 选项列表：标题 + 每项「数字 + 文案」+ 底部说明
        if self.options:
            oy = y + (20 if self.note else 0) + 2
            ot_font = QFont("Microsoft YaHei UI", 9)
            ot_font.setBold(True)
            p.setFont(ot_font)
            p.setPen(ACCENT)
            title = QFontMetrics(ot_font).elidedText(self.options_title, Qt.ElideRight,
                                                     int(self.width() - 36))
            p.drawText(QRectF(18, oy, self.width() - 34, 20), Qt.AlignLeft | Qt.AlignVCenter, title)
            oy += 20
            key_font = QFont("Consolas", 11)
            key_font.setBold(True)
            item_font = QFont("Microsoft YaHei UI", 9)
            for key, label in self.options:
                p.setFont(key_font)
                p.setPen(ACCENT)
                p.drawText(QRectF(18, oy, 22, 22), Qt.AlignLeft | Qt.AlignVCenter, key)
                p.setFont(item_font)
                p.setPen(ERROR if self.bad else VALUE)
                label = QFontMetrics(item_font).elidedText(label, Qt.ElideRight,
                                                           int(self.width() - 62))
                p.drawText(QRectF(44, oy, self.width() - 60, 22), Qt.AlignLeft | Qt.AlignVCenter, label)
                oy += 24
            if self.options_footer:
                ft_font = QFont("Microsoft YaHei UI", 9)
                p.setFont(ft_font)
                p.setPen(NOTE)
                foot = QFontMetrics(ft_font).elidedText(self.options_footer, Qt.ElideRight,
                                                       int(self.width() - 36))
                p.drawText(QRectF(18, oy - 2, self.width() - 34, 18),
                           Qt.AlignLeft | Qt.AlignVCenter, foot)

    def _fit_screen(self):
        """变宽 / 变高之后，别让浮窗跑到屏幕外面去（玩家可能把它摆在右边缘）。"""
        if not self.isVisible():
            return
        from PySide6.QtGui import QGuiApplication

        scr = QGuiApplication.screenAt(self.geometry().center()) or QGuiApplication.primaryScreen()
        if scr is None:
            return
        a = scr.availableGeometry()
        x, y = self.x(), self.y()
        nx = min(max(x, a.left() + 4), a.right() - self.width() - 4)
        ny = min(max(y, a.top() + 4), a.bottom() - self.height() - 4)
        if (nx, ny) != (x, y):
            self.move(nx, ny)

    # ---------- 交互 ----------
    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._drag = ev.globalPosition().toPoint() - self.frameGeometry().topLeft()
            ev.accept()

    def mouseMoveEvent(self, ev):
        if self._drag is not None and ev.buttons() & Qt.LeftButton:
            self.move(ev.globalPosition().toPoint() - self._drag)
            ev.accept()

    def mouseReleaseEvent(self, ev):
        if self._drag is not None:
            self._drag = None
            self.moved_by_user.emit(self.x(), self.y())

    def mouseDoubleClickEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self.show_main.emit()
            ev.accept()

    def contextMenuEvent(self, ev):
        menu = QMenu(self)
        act_main = QAction("显示主界面", menu)
        act_hide = QAction("隐藏浮窗", menu)
        act_reset = QAction("回到默认位置", menu)
        act_main.triggered.connect(self.show_main.emit)
        act_hide.triggered.connect(self.hide)
        act_reset.triggered.connect(self.reset_pos)
        menu.addAction(act_main)
        menu.addSeparator()
        menu.addAction(act_hide)
        menu.addAction(act_reset)
        menu.exec(ev.globalPos())

    def reset_pos(self):
        from PySide6.QtGui import QGuiApplication

        scr = QGuiApplication.primaryScreen().availableGeometry()
        self.move(scr.right() - self.width() - 40, scr.top() + 60)
        self.moved_by_user.emit(self.x(), self.y())

    def showEvent(self, ev):
        super().showEvent(ev)
        self.raise_()
