# Window frame shared by every page: header with status chip, navigation that is a
# bottom bar on narrow windows and a side rail on wide ones, and the building blocks
# the pages are laid out with. Layout depends on window width only, never platform.

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (QBoxLayout, QDialog, QDialogButtonBox, QFrame, QHBoxLayout,
                               QLabel, QSizePolicy, QStackedWidget, QToolButton,
                               QVBoxLayout, QWidget)

from ui import set_role

# Below this width (logical px) everything stacks in one column with a bottom bar.
BREAKPOINT = 700
RAIL_WIDTH = 76
SIDE_COLUMN_WIDTH = 360


def _draw(name, p, s):
    """Icon outlines on an s x s canvas."""
    c = s / 2
    if name == 'measure':  # a spectrum curve over a baseline
        path = QPainterPath(QPointF(s * .12, s * .78))
        path.cubicTo(s * .35, s * .78, s * .38, s * .2, s * .55, s * .2)
        path.cubicTo(s * .7, s * .2, s * .72, s * .78, s * .88, s * .78)
        p.drawPath(path)
    elif name == 'history':
        for y in (.28, .5, .72):
            p.drawLine(QPointF(s * .2, s * y), QPointF(s * .8, s * y))
    elif name == 'calibrate':  # sliders
        for y, x in ((.3, .62), (.7, .38)):
            p.drawLine(QPointF(s * .15, s * y), QPointF(s * .85, s * y))
            p.drawEllipse(QPointF(s * x, s * y), s * .09, s * .09)
    elif name == 'more':
        p.setBrush(p.pen().color())
        for x in (.25, .5, .75):
            p.drawEllipse(QPointF(s * x, c), s * .05, s * .05)
    elif name == 'back':
        p.drawPolyline([QPointF(s * .6, s * .22), QPointF(s * .32, c), QPointF(s * .6, s * .78)])
    elif name == 'chevron':
        p.drawPolyline([QPointF(s * .4, s * .25), QPointF(s * .65, c), QPointF(s * .4, s * .75)])
    elif name == 'zoom':
        p.drawEllipse(QPointF(s * .42, s * .42), s * .22, s * .22)
        p.drawLine(QPointF(s * .58, s * .58), QPointF(s * .82, s * .82))
    elif name == 'reset':
        p.drawArc(QRectF(s * .2, s * .2, s * .6, s * .6), 60 * 16, 300 * 16)
        p.drawPolyline([QPointF(s * .62, s * .12), QPointF(s * .66, s * .27), QPointF(s * .5, s * .3)])
    elif name == 'save':
        p.drawLine(QPointF(c, s * .15), QPointF(c, s * .62))
        p.drawPolyline([QPointF(s * .3, s * .45), QPointF(c, s * .65), QPointF(s * .7, s * .45)])
        p.drawLine(QPointF(s * .2, s * .82), QPointF(s * .8, s * .82))
    elif name == 'dot':
        p.setBrush(p.pen().color())
        p.drawEllipse(QPointF(c, c), s * .3, s * .3)


def icon(name, color, checked_color=None, size=48):
    """A QIcon painted in code (no image files to package); checked_color for toggles."""
    result = QIcon()
    for col, state in ((color, QIcon.State.Off), (checked_color or color, QIcon.State.On)):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        p = QPainter(pixmap)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(col), size * .08)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        _draw(name, p, size)
        p.end()
        result.addPixmap(pixmap, QIcon.Mode.Normal, state)
    return result


class Card(QFrame):
    """Rounded container; `body` is its layout."""

    def __init__(self, title=None):
        super().__init__()
        self.setObjectName('card')
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(14, 12, 14, 14)
        if title:
            label = QLabel(title)
            label.setStyleSheet('font-weight: 600;')
            self.body.addWidget(label)


class NavBar(QWidget):
    selected = Signal(int)

    def __init__(self):
        super().__init__()
        self.setObjectName('nav')
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self._layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self._layout.setContentsMargins(4, 4, 4, 4)
        self._buttons = []
        self._icons = []

    def add(self, label, icon_name):
        button = QToolButton()
        button.setObjectName('navButton')
        button.setText(label)
        button.setCheckable(True)
        button.setAutoExclusive(True)
        button.setIconSize(QSize(24, 24))
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        button.setMinimumHeight(56)
        index = len(self._buttons)
        button.clicked.connect(lambda: self.selected.emit(index))
        self._layout.addWidget(button)
        self._buttons.append(button)
        self._icons.append(icon_name)
        if index == 0:
            button.setChecked(True)

    def select(self, index):
        self._buttons[index].setChecked(True)

    def set_vertical(self, vertical):
        self._layout.setDirection(QBoxLayout.Direction.TopToBottom if vertical
                                  else QBoxLayout.Direction.LeftToRight)
        if vertical:
            self._layout.addStretch(1)
        else:
            stretch = self._layout.itemAt(self._layout.count() - 1)
            if stretch is not None and stretch.spacerItem() is not None:
                self._layout.takeAt(self._layout.count() - 1)
        self.setFixedWidth(RAIL_WIDTH) if vertical else self.setMaximumWidth(16777215)
        self.setMinimumWidth(RAIL_WIDTH if vertical else 0)

    def apply_theme(self, t):
        for button, name in zip(self._buttons, self._icons):
            button.setIcon(icon(name, t['muted'], t['text']))


class StatusChip(QToolButton):
    """'● Unit 1' in the header; its colour says connected, busy or not connected."""

    COLORS = {'connected': 'good', 'busy': 'warn', 'disconnected': 'muted'}

    def __init__(self):
        super().__init__()
        self.setObjectName('chip')
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setIconSize(QSize(10, 10))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._state = 'disconnected'
        self._tokens = None
        self.set_state('Not connected', 'disconnected')

    def set_state(self, text, state):
        self._state = state
        self.setText(text)
        self._repaint_dot()

    def apply_theme(self, t):
        self._tokens = t
        self._repaint_dot()

    def _repaint_dot(self):
        if self._tokens is not None:
            self.setIcon(icon('dot', self._tokens[self.COLORS[self._state]]))


class Sheet(QDialog):
    """A titled panel over the window for content that is only needed now and then."""

    def __init__(self, parent, title, content):
        super().__init__(parent)
        self.setWindowTitle(title)
        layout = QVBoxLayout(self)
        heading = QLabel(title)  # Android shows no title bar
        heading.setStyleSheet('font-weight: 600; font-size: 17px;')
        layout.addWidget(heading)
        layout.addWidget(content)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def fit(self):
        width = min(480, self.parentWidget().window().width() - 24)
        layout = self.layout()
        height = (layout.totalHeightForWidth(width) if layout.hasHeightForWidth()
                  else self.sizeHint().height())
        self.resize(width, max(height, self.sizeHint().height()))

    def open_fitted(self):
        self.fit()
        self.exec()


class _StepRow(QFrame):
    clicked = Signal()

    def __init__(self, title, summary):
        super().__init__()
        self.setObjectName('card')
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        row = QHBoxLayout(self)
        row.setContentsMargins(16, 12, 12, 12)
        text = QVBoxLayout()
        text.setSpacing(2)
        self.title = QLabel(title)
        self.title.setStyleSheet('font-weight: 600; font-size: 15px;')
        self.summary = QLabel(summary)
        self.summary.setWordWrap(True)
        set_role(self.summary, 'muted')
        text.addWidget(self.title)
        text.addWidget(self.summary)
        row.addLayout(text, 1)
        self.chevron = QLabel()
        row.addWidget(self.chevron)

    def mouseReleaseEvent(self, event):
        if self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class StepList(QWidget):
    """A list of rows; tapping one shows its page with a back arrow."""

    def __init__(self, intro=None, on_list_shown=None):
        super().__init__()
        self._on_list_shown = on_list_shown
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._stack = QStackedWidget()
        outer.addWidget(self._stack)

        self._list = QWidget()
        self._list_layout = QVBoxLayout(self._list)
        self._list_layout.setSpacing(10)
        if intro:
            label = QLabel(intro)
            label.setWordWrap(True)
            set_role(label, 'muted')
            self._list_layout.addWidget(label)
        self._list_layout.addStretch(1)
        self._stack.addWidget(self._list)
        self._rows = []
        self._back_buttons = []

    def list_widget(self):
        """The list itself, for the caller to wrap in a scroll area."""
        return self._list

    def set_list_container(self, container):
        self._stack.removeWidget(self._list)
        self._stack.insertWidget(0, container)
        self._stack.setCurrentIndex(0)

    def add(self, title, summary, page):
        row = _StepRow(title, summary)
        self._list_layout.insertWidget(self._list_layout.count() - 1, row)
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        header = QHBoxLayout()
        back = QToolButton()
        back.setObjectName('flat')
        back.setIconSize(QSize(22, 22))
        back.setFixedSize(40, 40)
        back.setAccessibleName('Back')
        back.clicked.connect(self.show_list)
        header.addWidget(back)
        title_label = QLabel(title)
        title_label.setStyleSheet('font-weight: 600; font-size: 17px;')
        header.addWidget(title_label, 1)
        layout.addLayout(header)
        layout.addWidget(page, 1)
        self._stack.addWidget(holder)
        row.clicked.connect(lambda: self._stack.setCurrentWidget(holder))
        self._rows.append(row)
        self._back_buttons.append(back)

    def set_summary(self, index, text):
        self._rows[index].summary.setText(text)

    def at_list(self):
        return self._stack.currentIndex() == 0

    def show_list(self):
        self._stack.setCurrentIndex(0)
        if self._on_list_shown is not None:
            self._on_list_shown()

    def apply_theme(self, t):
        for row in self._rows:
            row.chevron.setPixmap(icon('chevron', t['muted']).pixmap(18, 18))
        for back in self._back_buttons:
            back.setIcon(icon('back', t['text']))


class SplitPage(QWidget):
    """A scrolling column plus one `side` widget: beside the column when wide, inside it
    at `narrow_index` when narrow (then `narrow_height` decides its height)."""

    def __init__(self, column_scroll, column_layout, side, narrow_index, narrow_height):
        super().__init__()
        self._scroll = column_scroll
        self._column = column_layout
        self._side = side
        self._narrow_index = narrow_index
        self._narrow_height = narrow_height
        self._wide = None
        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.setSpacing(0)
        self._row.addWidget(self._scroll)
        self.set_wide(False)

    def set_wide(self, wide):
        if wide == self._wide:
            return
        self._wide = wide
        if wide:
            self._column.removeWidget(self._side)
            self._side.setMinimumHeight(0)
            self._side.setMaximumHeight(16777215)
            self._scroll.setFixedWidth(SIDE_COLUMN_WIDTH)
            self._row.addWidget(self._side, 1)
        else:
            self._row.removeWidget(self._side)
            self._scroll.setMinimumWidth(0)
            self._scroll.setMaximumWidth(16777215)
            self._column.insertWidget(self._narrow_index, self._side)
            self._fit_side()
        self._side.show()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not self._wide:
            self._fit_side()

    def _fit_side(self):
        self._side.setFixedHeight(max(260, self._narrow_height(self)))


class AppShell(QWidget):
    """Header, page stack and navigation; rearranges itself at BREAKPOINT."""

    wide_changed = Signal(bool)

    def __init__(self, title):
        super().__init__()
        self.stack = QStackedWidget()
        self.nav = NavBar()
        self.nav.selected.connect(self.stack.setCurrentIndex)
        self.chip = StatusChip()

        self.header = QWidget()
        header = QHBoxLayout(self.header)
        header.setContentsMargins(14, 8, 10, 4)
        title_label = QLabel(title)
        set_role(title_label, 'title')
        header.addWidget(title_label, 1)
        header.addWidget(self.chip)

        self._outer = QHBoxLayout(self)
        self._outer.setContentsMargins(0, 0, 0, 0)
        self._outer.setSpacing(0)
        self._main = QVBoxLayout()
        self._main.setContentsMargins(0, 0, 0, 0)
        self._main.setSpacing(0)
        self._main.addWidget(self.header)
        self._main.addWidget(self.stack, 1)
        self._outer.addLayout(self._main, 1)
        self._wide = None
        self._arrange(False)

    def add_page(self, label, icon_name, page):
        self.nav.add(label, icon_name)
        self.stack.addWidget(page)

    def select(self, index):
        self.nav.select(index)
        self.stack.setCurrentIndex(index)

    def is_wide(self):
        return bool(self._wide)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._arrange(self.width() >= BREAKPOINT)

    def _arrange(self, wide):
        if wide == self._wide:
            return
        self._wide = wide
        self._main.removeWidget(self.nav)
        self._outer.removeWidget(self.nav)
        self.nav.set_vertical(wide)
        if wide:
            self._outer.insertWidget(0, self.nav)
        else:
            self._main.addWidget(self.nav)
        self.wide_changed.emit(wide)

    def apply_theme(self, t):
        self.nav.apply_theme(t)
        self.chip.apply_theme(t)
