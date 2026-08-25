"""Volet « Croquis » : dessin 2D multi-vues pour exprimer une intention à l'agent.

L'utilisateur esquisse des formes approximatives dans 6 vues orthographiques
(face, arrière, haut, bas, gauche, droite). Le croquis est sérialisé en JSON
(mm, origine au centre de chaque vue, mapping axes 2D -> 3D par vue) et exposé
à l'agent via l'outil MCP `get_sketch`.
"""

import json
import math

import FreeCADGui as Gui

from PySide import QtCore, QtGui, QtWidgets

_dock = None

GRID_STEP = 10.0      # mm entre deux lignes fines de la grille
SNAP_STEP = 5.0       # mm d'aimantation
DEFAULT_DEPTH = 5.0   # mm : profondeur minimale donnée à chaque forme

# (clé, libellé, axe 3D horizontal, axe 3D vertical) — convention FreeCAD :
# vue de face = plan XZ avec +X vers la droite et +Z vers le haut.
VIEWS = [
    ("face",    "Face",    "+X", "+Z"),
    ("arriere", "Arrière", "-X", "+Z"),
    ("haut",    "Haut",    "+X", "+Y"),
    ("bas",     "Bas",     "+X", "-Y"),
    ("gauche",  "Gauche",  "-Y", "+Z"),
    ("droite",  "Droite",  "+Y", "+Z"),
]

_VIEW_AXES = {key: (h_axis, v_axis) for key, _label, h_axis, v_axis in VIEWS}

TOOLS = [
    ("select",    "Sélection", "Sélectionner / déplacer (Suppr pour effacer)"),
    ("ligne",     "Ligne",     "Tracer une ligne (cliquer-glisser)"),
    ("rectangle", "Rect",      "Tracer un rectangle (cliquer-glisser)"),
    ("cercle",    "Cercle",    "Tracer un cercle (centre puis glisser le rayon)"),
    ("ellipse",   "Ellipse",   "Tracer une ellipse (cliquer-glisser)"),
    ("polygone",  "Polygone",  "Polygone régulier (centre puis glisser le rayon)"),
    ("note",      "Note",      "Placer une annotation texte"),
]

_PEN = QtGui.QPen(QtGui.QColor("#1d66cc"), 2)          # plein : bleu
_PEN.setCosmetic(True)

_HOLE_PEN = QtGui.QPen(QtGui.QColor("#cc2222"), 2)     # trou : rouge
_HOLE_PEN.setCosmetic(True)

# Fantômes : projection des formes des autres vues (non sélectionnables).
_GHOST_PEN = QtGui.QPen(QtGui.QColor("#a9b6c4"), 1, QtCore.Qt.DashLine)
_GHOST_PEN.setCosmetic(True)

_GHOST_HOLE_PEN = QtGui.QPen(QtGui.QColor("#d8a9a9"), 1, QtCore.Qt.DashLine)
_GHOST_HOLE_PEN.setCosmetic(True)

# Poignées de redimensionnement (coins de la forme sélectionnée).
_HANDLE_PEN = QtGui.QPen(QtGui.QColor("#1d66cc"), 1)
_HANDLE_PEN.setCosmetic(True)
_HANDLE_BRUSH = QtGui.QBrush(QtGui.QColor("#ffffff"))

# Boîte englobante 3D de l'objet, projetée dans chaque vue.
_BOX_PEN = QtGui.QPen(QtGui.QColor("#d9a066"), 1, QtCore.Qt.DashDotLine)
_BOX_PEN.setCosmetic(True)

# Vecteurs unitaires 3D des axes de vue.
_AXIS = {
    "+X": (1, 0, 0), "-X": (-1, 0, 0),
    "+Y": (0, 1, 0), "-Y": (0, -1, 0),
    "+Z": (0, 0, 1), "-Z": (0, 0, -1),
}


def _cross(a, b):
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _round1(value):
    return round(float(value), 1)


def _pt(point):
    return [_round1(point.x()), _round1(point.y())]


def _item_outline(item):
    """Contour d'une forme en points de scène : (points, fermé) ou (None, False)."""
    tool = item.data(0)
    if tool == "ligne":
        line = item.line()
        return [item.mapToScene(line.p1()), item.mapToScene(line.p2())], False
    if tool == "rectangle":
        return list(item.mapToScene(item.rect())), True
    if tool in ("cercle", "ellipse"):
        rect = item.rect()
        points = []
        for i in range(36):
            angle = 2 * math.pi * i / 36
            points.append(
                item.mapToScene(
                    QtCore.QPointF(
                        rect.center().x() + rect.width() / 2 * math.cos(angle),
                        rect.center().y() + rect.height() / 2 * math.sin(angle),
                    )
                )
            )
        return points, True
    if tool == "polygone":
        return list(item.mapToScene(item.polygon())), True
    return None, False  # notes : pas de fantôme


class SketchScene(QtWidgets.QGraphicsScene):
    """Scène avec grille millimétrée et axes passant par l'origine."""

    def __init__(self, parent=None):
        super().__init__(-1000, -1000, 2000, 2000, parent)

    def drawBackground(self, painter, rect):
        painter.fillRect(rect, QtGui.QColor("#ffffff"))
        minor = QtGui.QPen(QtGui.QColor("#ececec"))
        minor.setCosmetic(True)
        major = QtGui.QPen(QtGui.QColor("#d5d5d5"))
        major.setCosmetic(True)
        axis = QtGui.QPen(QtGui.QColor("#9bb7d4"), 2)
        axis.setCosmetic(True)

        first = int(math.floor(rect.left() / GRID_STEP))
        last = int(math.ceil(rect.right() / GRID_STEP))
        for i in range(first, last + 1):
            x = i * GRID_STEP
            painter.setPen(major if i % 5 == 0 else minor)
            painter.drawLine(QtCore.QLineF(x, rect.top(), x, rect.bottom()))
        first = int(math.floor(rect.top() / GRID_STEP))
        last = int(math.ceil(rect.bottom() / GRID_STEP))
        for i in range(first, last + 1):
            y = i * GRID_STEP
            painter.setPen(major if i % 5 == 0 else minor)
            painter.drawLine(QtCore.QLineF(rect.left(), y, rect.right(), y))

        painter.setPen(axis)
        painter.drawLine(QtCore.QLineF(rect.left(), 0, rect.right(), 0))
        painter.drawLine(QtCore.QLineF(0, rect.top(), 0, rect.bottom()))


class SketchView(QtWidgets.QGraphicsView):
    """Une vue orthographique : dessin, sélection, zoom molette, pan bouton milieu."""

    def __init__(self, scene, panel, view_key, view_label):
        super().__init__(scene)
        self.panel = panel
        self.view_key = view_key
        self.view_label = view_label
        self._temp_item = None
        self._press_scene_pos = None
        self._panning = False
        self._pan_last = None
        self._thickness_drag = None  # drag d'un fantôme = donner de l'épaisseur
        self._resize_drag = None     # drag d'une poignée = redimensionner

        self.setRenderHint(QtGui.QPainter.Antialiasing)
        self.setTransformationAnchor(QtWidgets.QGraphicsView.AnchorUnderMouse)
        self.setMouseTracking(True)
        # Repère main droite : v (vertical) positif vers le haut de l'écran.
        self.setTransform(QtGui.QTransform.fromScale(1.5, -1.5))
        self.setMinimumSize(180, 140)

    # --- aides ------------------------------------------------------------

    def _snap(self, point):
        if not self.panel.snap_check.isChecked():
            return point
        return QtCore.QPointF(
            round(point.x() / SNAP_STEP) * SNAP_STEP,
            round(point.y() / SNAP_STEP) * SNAP_STEP,
        )

    def _finalize_item(self, item, tool):
        item.setData(0, tool)
        item.setFlag(QtWidgets.QGraphicsItem.ItemIsSelectable, True)
        item.setFlag(QtWidgets.QGraphicsItem.ItemIsMovable, True)
        if tool != "note":
            item.setData(5, "trou" if self.panel.hole_btn.isChecked() else "plein")
            item.setData(2, (0.0, DEFAULT_DEPTH))

    def _cancel_temp(self):
        if self._temp_item is not None:
            self.scene().removeItem(self._temp_item)
            self._temp_item = None
        self._press_scene_pos = None

    def _handle_at(self, view_pos):
        rect = QtCore.QRect(view_pos.x() - 6, view_pos.y() - 6, 12, 12)
        for item in self.items(rect):
            if item.data(1) == "handle":
                return item
        return None

    def _begin_resize_drag(self, handle, _event):
        """Démarre le redimensionnement : la poignée suit, le coin opposé est ancré."""
        target = handle.data(3)
        corner_idx = handle.data(4)
        rect = target.sceneBoundingRect()
        corners = [
            rect.topLeft(), rect.topRight(), rect.bottomLeft(), rect.bottomRight()
        ]
        tool = target.data(0)
        if tool == "ligne":
            orig = QtCore.QLineF(target.line())
        elif tool == "polygone":
            orig = QtGui.QPolygonF(target.polygon())
        else:
            orig = QtCore.QRectF(target.rect())
        self._resize_drag = {
            "item": target,
            "tool": tool,
            "anchor": corners[3 - corner_idx],  # coin opposé
            "start": corners[corner_idx],
            "orig": orig,
            "pos": QtCore.QPointF(target.pos()),
        }
        return True

    def _ghost_at(self, view_pos):
        """Fantôme sous le curseur (±6 px) — None si une vraie forme est au-dessus."""
        rect = QtCore.QRect(view_pos.x() - 6, view_pos.y() - 6, 12, 12)
        for item in self.items(rect):
            if item.data(0):
                return None
            if item.data(1) == "ghost" and item.data(3) is not None:
                return item
        return None

    def _begin_thickness_drag(self, ghost, event):
        """Démarre le réglage d'épaisseur en tirant un fantôme le long de la normale."""
        src_item = ghost.data(3)
        src_key = ghost.data(4)
        h_axis, v_axis = _VIEW_AXES[src_key]
        normal = _cross(_AXIS[h_axis], _AXIS[v_axis])
        my_h, my_v = _VIEW_AXES[self.view_key]
        n_h = _dot(normal, _AXIS[my_h])
        n_v = _dot(normal, _AXIS[my_v])
        if n_h == 0 and n_v == 0:
            return False  # vue parallèle à la forme : l'épaisseur ne s'y voit pas
        pos = self._snap(self.mapToScene(event.pos()))
        sign = n_h or n_v
        d_press = (pos.x() if n_h else pos.y()) / sign
        d0, d1 = src_item.data(2) or (0.0, 0.0)
        # Ancre = la borne la plus éloignée du clic : on tire la borne proche.
        far = d0 if abs(d_press - d0) >= abs(d_press - d1) else d1
        self._thickness_drag = {
            "item": src_item,
            "use_h": bool(n_h),
            "sign": sign,
            "far": far,
        }
        return True

    def fit_content(self):
        rect = self.scene().itemsBoundingRect()
        if rect.isEmpty():
            rect = QtCore.QRectF(-100, -100, 200, 200)
        self.fitInView(rect.adjusted(-10, -10, 10, 10), QtCore.Qt.KeepAspectRatio)

    def delete_selection(self):
        removed = False
        for item in self.scene().selectedItems():
            self.scene().removeItem(item)
            removed = True
        if removed:
            self.panel.refresh_ghosts()

    # --- événements souris ------------------------------------------------

    def mousePressEvent(self, event):
        self.panel.set_active_view(self)
        if event.button() == QtCore.Qt.MiddleButton:
            self._panning = True
            self._pan_last = event.pos()
            self.setCursor(QtCore.Qt.ClosedHandCursor)
            return
        tool = self.panel.tool
        if event.button() != QtCore.Qt.LeftButton or tool == "select":
            if event.button() == QtCore.Qt.LeftButton and tool == "select":
                handle = self._handle_at(event.pos())
                if handle is not None and self._begin_resize_drag(handle, event):
                    return
                ghost = self._ghost_at(event.pos())
                if ghost is not None and self._begin_thickness_drag(ghost, event):
                    return
            self.setDragMode(QtWidgets.QGraphicsView.RubberBandDrag)
            super().mousePressEvent(event)
            return
        self.setDragMode(QtWidgets.QGraphicsView.NoDrag)
        pos = self._snap(self.mapToScene(event.pos()))
        self._press_scene_pos = pos
        pen = _HOLE_PEN if self.panel.hole_btn.isChecked() else _PEN
        if tool == "ligne":
            self._temp_item = self.scene().addLine(QtCore.QLineF(pos, pos), pen)
        elif tool in ("rectangle", "cercle", "ellipse"):
            rect = QtCore.QRectF(pos, pos)
            factory = (
                self.scene().addRect if tool == "rectangle" else self.scene().addEllipse
            )
            self._temp_item = factory(rect, pen)
        elif tool == "polygone":
            self._temp_item = self.scene().addPolygon(QtGui.QPolygonF(), pen)
        elif tool == "note":
            self._press_scene_pos = None
            text, ok = QtWidgets.QInputDialog.getText(
                self, "Note", "Texte de l'annotation :"
            )
            if ok and text.strip():
                item = self.scene().addSimpleText(text.strip())
                item.setBrush(QtGui.QBrush(QtGui.QColor("#b3541e")))
                font = item.font()
                font.setPixelSize(8)
                item.setFont(font)
                # La vue est inversée verticalement : re-inverser le texte.
                item.setTransform(QtGui.QTransform.fromScale(1, -1))
                item.setPos(pos)
                self._finalize_item(item, "note")

    def mouseMoveEvent(self, event):
        scene_pos = self.mapToScene(event.pos())
        self.panel.set_status_pos(self, scene_pos)
        if self._panning:
            delta = event.pos() - self._pan_last
            self._pan_last = event.pos()
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - delta.x()
            )
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - delta.y()
            )
            return
        if self._resize_drag is not None:
            drag = self._resize_drag
            pos = self._snap(scene_pos)
            anchor_x, anchor_y = drag["anchor"].x(), drag["anchor"].y()
            span_x = drag["start"].x() - anchor_x
            span_y = drag["start"].y() - anchor_y
            sx = (pos.x() - anchor_x) / span_x if abs(span_x) > 1e-6 else 1.0
            sy = (pos.y() - anchor_y) / span_y if abs(span_y) > 1e-6 else 1.0
            if drag["tool"] == "cercle":
                # Homothétie : un cercle reste un cercle.
                sx = sy = abs(sx if abs(sx - 1.0) >= abs(sy - 1.0) else sy)
            item_x, item_y = drag["pos"].x(), drag["pos"].y()

            def scaled(p):
                return QtCore.QPointF(
                    anchor_x - item_x + (p.x() + item_x - anchor_x) * sx,
                    anchor_y - item_y + (p.y() + item_y - anchor_y) * sy,
                )

            item = drag["item"]
            if drag["tool"] == "ligne":
                item.setLine(
                    QtCore.QLineF(scaled(drag["orig"].p1()), scaled(drag["orig"].p2()))
                )
            elif drag["tool"] == "polygone":
                item.setPolygon(QtGui.QPolygonF([scaled(p) for p in drag["orig"]]))
            else:
                item.setRect(
                    QtCore.QRectF(
                        scaled(drag["orig"].topLeft()),
                        scaled(drag["orig"].bottomRight()),
                    ).normalized()
                )
            self.panel.refresh_ghosts()
            size = item.sceneBoundingRect()
            self.panel.status.setText(
                "Dimensions : %.1f × %.1f mm" % (size.width(), size.height())
            )
            return
        if self._thickness_drag is not None:
            drag = self._thickness_drag
            pos = self._snap(scene_pos)
            d_now = (pos.x() if drag["use_h"] else pos.y()) / drag["sign"]
            bounds = (min(drag["far"], d_now), max(drag["far"], d_now))
            drag["item"].setData(2, bounds)
            drag["item"].setData(6, True)  # profondeur réglée par l'utilisateur
            self.panel.refresh_ghosts()
            self.panel.status.setText(
                "Épaisseur : %.1f mm" % (bounds[1] - bounds[0])
            )
            return
        if self._temp_item is None or self._press_scene_pos is None:
            super().mouseMoveEvent(event)
            return
        pos = self._snap(scene_pos)
        origin = self._press_scene_pos
        tool = self.panel.tool
        if tool == "ligne":
            self._temp_item.setLine(QtCore.QLineF(origin, pos))
        elif tool in ("rectangle", "ellipse"):
            self._temp_item.setRect(QtCore.QRectF(origin, pos).normalized())
        elif tool == "cercle":
            radius = math.hypot(pos.x() - origin.x(), pos.y() - origin.y())
            self._temp_item.setRect(
                origin.x() - radius, origin.y() - radius, 2 * radius, 2 * radius
            )
        elif tool == "polygone":
            radius = math.hypot(pos.x() - origin.x(), pos.y() - origin.y())
            sides = self.panel.sides_spin.value()
            points = QtGui.QPolygonF()
            for i in range(sides):
                angle = math.pi / 2 + 2 * math.pi * i / sides
                points.append(
                    QtCore.QPointF(
                        origin.x() + radius * math.cos(angle),
                        origin.y() + radius * math.sin(angle),
                    )
                )
            self._temp_item.setPolygon(points)

    def mouseReleaseEvent(self, event):
        if event.button() == QtCore.Qt.MiddleButton and self._panning:
            self._panning = False
            self.setCursor(QtCore.Qt.ArrowCursor)
            return
        if self._resize_drag is not None:
            self._resize_drag = None
            self.panel.refresh_ghosts()
            return
        if self._thickness_drag is not None:
            self._thickness_drag = None
            self.panel.refresh_ghosts()
            return
        if self._temp_item is None:
            super().mouseReleaseEvent(event)
            # Un déplacement en mode sélection a pu changer les projections.
            self.panel.refresh_ghosts()
            return
        item = self._temp_item
        tool = self.panel.tool
        self._temp_item = None
        self._press_scene_pos = None
        degenerate = False
        if tool == "ligne":
            degenerate = item.line().length() < 1.0
        elif tool in ("rectangle", "ellipse", "cercle"):
            rect = item.rect()
            degenerate = rect.width() < 1.0 and rect.height() < 1.0
        elif tool == "polygone":
            degenerate = item.polygon().boundingRect().width() < 1.0
        if degenerate:
            self.scene().removeItem(item)
        else:
            self._finalize_item(item, tool)
            self.panel.refresh_ghosts()

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if not delta:
            return
        factor = 1.15 if delta > 0 else 1 / 1.15
        self.scale(factor, factor)
        self.panel.sync_views(self)

    def keyPressEvent(self, event):
        if event.key() in (QtCore.Qt.Key_Delete, QtCore.Qt.Key_Backspace):
            self.delete_selection()
            return
        if event.key() == QtCore.Qt.Key_Escape:
            self._cancel_temp()
            return
        super().keyPressEvent(event)


class SketchPanel(QtWidgets.QDockWidget):
    def __init__(self, parent=None):
        super().__init__("Croquis Claude", parent)
        self.setObjectName("ClaudeSketchPanel")
        self.tool = "select"
        self.scenes = {}
        self.views = {}
        self.active_view = None
        self._syncing = False
        self._updating_handles = False
        # Point 3D que toutes les vues regardent (mis à jour au pan/zoom).
        self._focus3d = [0.0, 0.0, 0.0]

        body = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(body)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # Rangée 1 : outils de dessin (exclusifs)
        tools_row = QtWidgets.QHBoxLayout()
        self._tool_group = QtWidgets.QButtonGroup(self)
        self._tool_group.setExclusive(True)
        for key, label, tip in TOOLS:
            btn = QtWidgets.QToolButton()
            btn.setText(label)
            btn.setToolTip(tip)
            btn.setCheckable(True)
            btn.setChecked(key == self.tool)
            btn.clicked.connect(lambda _=False, k=key: self._set_tool(k))
            self._tool_group.addButton(btn)
            tools_row.addWidget(btn)
        self.sides_spin = QtWidgets.QSpinBox()
        self.sides_spin.setRange(3, 12)
        self.sides_spin.setValue(6)
        self.sides_spin.setToolTip("Nombre de côtés du polygone")
        tools_row.addWidget(QtWidgets.QLabel("côtés :"))
        tools_row.addWidget(self.sides_spin)
        self.hole_btn = QtWidgets.QToolButton()
        self.hole_btn.setText("Trou")
        self.hole_btn.setCheckable(True)
        self.hole_btn.setToolTip(
            "Coché : les nouvelles formes sont des trous (rouge, matière à "
            "enlever). Décoché : des pleins (bleu, matière à ajouter)."
        )
        self.hole_btn.setStyleSheet(
            "QToolButton:checked { color: #cc2222; font-weight: bold; }"
        )
        tools_row.addWidget(self.hole_btn)
        tools_row.addStretch()
        layout.addLayout(tools_row)

        # Rangée 2 : actions
        actions_row = QtWidgets.QHBoxLayout()
        delete_btn = QtWidgets.QPushButton("Supprimer")
        delete_btn.setToolTip("Supprimer la sélection de la vue active")
        delete_btn.clicked.connect(self._delete_selection)
        clear_btn = QtWidgets.QPushButton("Tout effacer")
        clear_btn.clicked.connect(self._clear_all)
        nature_btn = QtWidgets.QPushButton("Plein⇄Trou")
        nature_btn.setToolTip("Inverser plein/trou pour les formes sélectionnées")
        nature_btn.clicked.connect(self._toggle_nature_selection)
        zoom_out = QtWidgets.QPushButton("−")
        zoom_out.setFixedWidth(28)
        zoom_out.clicked.connect(lambda: self._zoom(1 / 1.3))
        zoom_in = QtWidgets.QPushButton("+")
        zoom_in.setFixedWidth(28)
        zoom_in.clicked.connect(lambda: self._zoom(1.3))
        fit_btn = QtWidgets.QPushButton("Adapter")
        fit_btn.setToolTip("Ajuster le zoom de la vue active au contenu")
        fit_btn.clicked.connect(self._fit_active)
        self.snap_check = QtWidgets.QCheckBox("Aimanter (%g mm)" % SNAP_STEP)
        self.snap_check.setChecked(True)
        for widget in (
            delete_btn, clear_btn, nature_btn,
            zoom_out, zoom_in, fit_btn, self.snap_check,
        ):
            actions_row.addWidget(widget)
        actions_row.addStretch()
        layout.addLayout(actions_row)

        # Grille 2x3 des six vues
        grid_widget = QtWidgets.QWidget()
        grid = QtWidgets.QGridLayout(grid_widget)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(4)
        for index, (key, label, h_axis, v_axis) in enumerate(VIEWS):
            scene = SketchScene(self)
            view = SketchView(scene, self, key, label)
            scene.selectionChanged.connect(self.refresh_handles)
            self.scenes[key] = scene
            self.views[key] = view
            cell = QtWidgets.QVBoxLayout()
            cell.setSpacing(1)
            header = QtWidgets.QLabel(
                "<b>%s</b> <span style='color:gray;'>(→%s ↑%s)</span>"
                % (label, h_axis, v_axis)
            )
            cell.addWidget(header)
            cell.addWidget(view, stretch=1)
            holder = QtWidgets.QWidget()
            holder.setLayout(cell)
            grid.addWidget(holder, index // 3, index % 3)
        layout.addWidget(grid_widget, stretch=1)

        # Zoom et défilement synchronisés entre les six vues.
        for view in self.views.values():
            view.horizontalScrollBar().valueChanged.connect(
                lambda _=0, v=view: self.sync_views(v)
            )
            view.verticalScrollBar().valueChanged.connect(
                lambda _=0, v=view: self.sync_views(v)
            )

        self.status = QtWidgets.QLabel(
            "Dessine dans une vue ; tire un fantôme pointillé (vue "
            "perpendiculaire, mode Sélection) pour donner de l'épaisseur."
        )
        self.status.setStyleSheet("color: gray;")
        layout.addWidget(self.status)

        self.setWidget(body)
        self.active_view = self.views[VIEWS[0][0]]

    # --- outils / actions -------------------------------------------------

    def _set_tool(self, key):
        self.tool = key
        mode = (
            QtWidgets.QGraphicsView.RubberBandDrag
            if key == "select"
            else QtWidgets.QGraphicsView.NoDrag
        )
        for view in self.views.values():
            view.setDragMode(mode)

    def set_active_view(self, view):
        self.active_view = view

    def set_status_pos(self, view, pos):
        self.status.setText(
            "Vue %s : h=%.1f mm, v=%.1f mm" % (view.view_label, pos.x(), pos.y())
        )

    def _delete_selection(self):
        for view in self.views.values():
            view.delete_selection()

    def _toggle_nature_selection(self):
        changed = False
        for scene in self.scenes.values():
            for item in scene.selectedItems():
                nature = item.data(5)
                if not nature:
                    continue  # notes : pas de nature
                new_nature = "plein" if nature == "trou" else "trou"
                item.setData(5, new_nature)
                item.setPen(_HOLE_PEN if new_nature == "trou" else _PEN)
                changed = True
        if changed:
            self.refresh_ghosts()

    def _clear_all(self):
        answer = QtWidgets.QMessageBox.question(
            self, "Croquis", "Effacer les formes des six vues ?"
        )
        if answer == QtWidgets.QMessageBox.Yes:
            for scene in self.scenes.values():
                scene.clear()

    def _zoom(self, factor):
        if self.active_view is not None:
            self.active_view.scale(factor, factor)
            self.sync_views(self.active_view)

    def _fit_active(self):
        if self.active_view is not None:
            self.active_view.fit_content()
            self.sync_views(self.active_view)

    def sync_views(self, src_view):
        """Toutes les vues regardent le même point 3D, chacune selon ses axes.

        Le centre visible de src_view met à jour les deux composantes 3D
        correspondant à ses axes (la troisième est conservée) ; les autres
        vues reprennent le même zoom et se recentrent sur la projection de ce
        point 3D dans leur propre repère.
        """
        if self._syncing:
            return
        self._syncing = True
        try:
            center = src_view.mapToScene(src_view.viewport().rect().center())
            h_axis, v_axis = _VIEW_AXES[src_view.view_key]
            for axis_name, coord in ((h_axis, center.x()), (v_axis, center.y())):
                unit = _AXIS[axis_name]
                index = next(i for i in range(3) if unit[i])
                self._focus3d[index] = unit[index] * coord
            transform = src_view.transform()
            for view in self.views.values():
                if view is src_view:
                    continue
                view.setTransform(transform)
                dst_h, dst_v = _VIEW_AXES[view.view_key]
                view.centerOn(
                    _dot(_AXIS[dst_h], self._focus3d),
                    _dot(_AXIS[dst_v], self._focus3d),
                )
        finally:
            self._syncing = False

    # --- projections fantômes --------------------------------------------

    def refresh_ghosts(self):
        """Reprojette chaque forme dans les 5 autres vues + boîte englobante 3D.

        Une forme est supposée dans le plan de sa vue, décalée/extrudée le long
        de la normale selon son épaisseur (data(2) = (d0, d1), réglée en tirant
        un fantôme). Vue opposée : contour miroir ; vues perpendiculaires :
        rectangle (silhouette de l'extrusion), réduit à un segment si
        l'épaisseur est nulle.
        """
        for scene in self.scenes.values():
            for item in list(scene.items()):
                if item.data(1) == "ghost":
                    scene.removeItem(item)
        all_points_3d = []
        for src_key, _, h_axis, v_axis in VIEWS:
            h_unit, v_unit = _AXIS[h_axis], _AXIS[v_axis]
            n_unit = _cross(h_unit, v_unit)
            for item in list(self.scenes[src_key].items()):
                if item.data(1) == "ghost" or not item.data(0):
                    continue
                points, closed = _item_outline(item)
                if not points:
                    continue
                d0, d1 = item.data(2) or (0.0, 0.0)
                layers = []
                for depth in ((d0,) if d0 == d1 else (d0, d1)):
                    layers.append([
                        tuple(
                            h_unit[i] * p.x() + v_unit[i] * p.y() + n_unit[i] * depth
                            for i in range(3)
                        )
                        for p in points
                    ])
                for layer in layers:
                    all_points_3d.extend(layer)
                for dst_key, _, dst_h, dst_v in VIEWS:
                    if dst_key == src_key:
                        continue
                    dh_unit, dv_unit = _AXIS[dst_h], _AXIS[dst_v]
                    projected = [
                        [(_dot(dh_unit, p3), _dot(dv_unit, p3)) for p3 in layer]
                        for layer in layers
                    ]
                    path = QtGui.QPainterPath()
                    if _dot(n_unit, dh_unit) == 0 and _dot(n_unit, dv_unit) == 0:
                        # Vue parallèle : contour (l'épaisseur ne s'y voit pas).
                        first = projected[0]
                        path.moveTo(first[0][0], first[0][1])
                        for h2, v2 in first[1:]:
                            path.lineTo(h2, v2)
                        if closed:
                            path.closeSubpath()
                    else:
                        # Vue perpendiculaire : silhouette de l'extrusion.
                        flat = [pt for layer in projected for pt in layer]
                        hs = [pt[0] for pt in flat]
                        vs = [pt[1] for pt in flat]
                        rect = QtCore.QRectF(
                            QtCore.QPointF(min(hs), min(vs)),
                            QtCore.QPointF(max(hs), max(vs)),
                        )
                        if rect.width() < 0.01 or rect.height() < 0.01:
                            path.moveTo(rect.topLeft())
                            path.lineTo(rect.bottomRight())
                        else:
                            path.addRect(rect)
                    ghost_pen = (
                        _GHOST_HOLE_PEN if item.data(5) == "trou" else _GHOST_PEN
                    )
                    ghost = self.scenes[dst_key].addPath(path, ghost_pen)
                    ghost.setData(1, "ghost")
                    ghost.setData(3, item)     # source du drag d'épaisseur
                    ghost.setData(4, src_key)
                    ghost.setZValue(-1)
        self._draw_bounding_box(all_points_3d)
        self.refresh_handles()

    def refresh_handles(self):
        """Poignées d'angle sur chaque forme sélectionnée (taille écran fixe)."""
        if self._updating_handles:
            return
        self._updating_handles = True
        try:
            for scene in self.scenes.values():
                for item in list(scene.items()):
                    if item.data(1) == "handle":
                        scene.removeItem(item)
            for scene in self.scenes.values():
                for item in scene.selectedItems():
                    if not item.data(0) or item.data(0) == "note":
                        continue
                    rect = item.sceneBoundingRect()
                    corners = [
                        rect.topLeft(), rect.topRight(),
                        rect.bottomLeft(), rect.bottomRight(),
                    ]
                    for corner_idx, corner in enumerate(corners):
                        handle = scene.addRect(
                            QtCore.QRectF(-4, -4, 8, 8), _HANDLE_PEN, _HANDLE_BRUSH
                        )
                        handle.setFlag(
                            QtWidgets.QGraphicsItem.ItemIgnoresTransformations, True
                        )
                        handle.setPos(corner)
                        handle.setData(1, "handle")
                        handle.setData(3, item)
                        handle.setData(4, corner_idx)
                        handle.setZValue(10)
        finally:
            self._updating_handles = False

    def _draw_bounding_box(self, points_3d):
        """Projette la boîte englobante 3D de l'objet dans chaque vue."""
        if not points_3d:
            return
        xs = [p[0] for p in points_3d]
        ys = [p[1] for p in points_3d]
        zs = [p[2] for p in points_3d]
        corners = [
            (x, y, z)
            for x in (min(xs), max(xs))
            for y in (min(ys), max(ys))
            for z in (min(zs), max(zs))
        ]
        for key, _, dst_h, dst_v in VIEWS:
            dh_unit, dv_unit = _AXIS[dst_h], _AXIS[dst_v]
            hs = [_dot(dh_unit, c) for c in corners]
            vs = [_dot(dv_unit, c) for c in corners]
            rect = QtCore.QRectF(
                QtCore.QPointF(min(hs), min(vs)), QtCore.QPointF(max(hs), max(vs))
            )
            box = self.scenes[key].addRect(rect, _BOX_PEN)
            box.setData(1, "ghost")
            box.setZValue(-2)

    # --- sérialisation ----------------------------------------------------

    def has_content(self):
        return any(
            item.data(0)
            for scene in self.scenes.values()
            for item in scene.items()
        )

    def serialize(self):
        views = {}
        for key, label, h_axis, v_axis in VIEWS:
            n_unit = _cross(_AXIS[h_axis], _AXIS[v_axis])
            axis_index = next(i for i in range(3) if n_unit[i])
            shapes = []
            for item in self.scenes[key].items():
                shape = self._serialize_item(item)
                if shape is None:
                    continue
                if item.data(5):
                    shape["nature"] = item.data(5)
                d0, d1 = item.data(2) or (0.0, 0.0)
                if d0 or d1:
                    world = sorted((n_unit[axis_index] * d0, n_unit[axis_index] * d1))
                    shape["profondeur"] = {
                        "axe": "XYZ"[axis_index],
                        "min": _round1(world[0]),
                        "max": _round1(world[1]),
                    }
                    if not item.data(6):
                        shape["profondeur"]["defaut"] = True
                shapes.append(shape)
            if shapes:
                shapes.reverse()  # items() renvoie l'ordre inverse d'ajout
                views[key] = {
                    "libelle": label,
                    "axes": {"h": h_axis, "v": v_axis},
                    "shapes": shapes,
                }
        return {
            "unit": "mm",
            "origine": "centre de chaque vue (croix des axes)",
            "convention": (
                "Coordonnées [h, v] : h suit axes.h, v suit axes.v (v positif "
                "vers le haut de la vue). Chaque forme porte 'nature' : "
                "'plein' = matière à ajouter (pad/extrusion), 'trou' = matière "
                "à enlever (pocket/perçage à soustraire du plein). 'profondeur' "
                "(axe 3D, min, max) donne son épaisseur/extrusion le long du "
                "troisième axe ; si 'defaut' est vrai, c'est la valeur "
                "minimale (%g mm) non réglée par l'utilisateur : choisis une "
                "profondeur sensée (p.ex. débouchant pour un trou). Croquis "
                "approximatif : interprète l'intention, arrondis les cotes à "
                "des valeurs plausibles."
                % DEFAULT_DEPTH
            ),
            "views": views,
        }

    @staticmethod
    def _serialize_item(item):
        tool = item.data(0)
        if tool == "ligne":
            line = item.line()
            return {
                "type": "ligne",
                "de": _pt(item.mapToScene(line.p1())),
                "a": _pt(item.mapToScene(line.p2())),
            }
        if tool in ("rectangle", "cercle", "ellipse"):
            rect = item.mapToScene(item.rect()).boundingRect()
            center = _pt(rect.center())
            if tool == "rectangle":
                return {
                    "type": "rectangle",
                    "centre": center,
                    "largeur": _round1(rect.width()),
                    "hauteur": _round1(rect.height()),
                }
            if tool == "cercle":
                return {
                    "type": "cercle",
                    "centre": center,
                    "rayon": _round1((rect.width() + rect.height()) / 4),
                }
            return {
                "type": "ellipse",
                "centre": center,
                "rayon_h": _round1(rect.width() / 2),
                "rayon_v": _round1(rect.height() / 2),
            }
        if tool == "polygone":
            points = item.mapToScene(item.polygon())
            return {
                "type": "polygone",
                "points": [_pt(p) for p in points],
            }
        if tool == "note":
            return {
                "type": "note",
                "position": _pt(item.scenePos()),
                "texte": item.text(),
            }
        return None


# --- API module ------------------------------------------------------------


def show_panel():
    global _dock
    main_window = Gui.getMainWindow()
    if _dock is None:
        _dock = SketchPanel(main_window)
        main_window.addDockWidget(QtCore.Qt.RightDockWidgetArea, _dock)
        # Même volet que le chat : les deux docks partagent des onglets.
        from . import panel as chat_panel

        if chat_panel._dock is not None:
            main_window.tabifyDockWidget(chat_panel._dock, _dock)
    _dock.show()
    _dock.raise_()


def toggle_panel():
    if _dock is not None and _dock.isVisible():
        _dock.hide()
    else:
        show_panel()


def get_sketch_json():
    """JSON du croquis courant, ou None si le volet est fermé/vide."""
    if _dock is None or not _dock.has_content():
        return None
    return json.dumps(_dock.serialize(), ensure_ascii=False, indent=1)


def prompt_hint():
    """Indice à joindre au prompt quand un croquis non vide existe."""
    if _dock is None or not _dock.has_content():
        return ""
    return (
        "[Contexte : l'utilisateur a dessiné un croquis dans le volet Croquis "
        "de FreeCAD — appelle l'outil get_sketch pour le consulter avant de "
        "modéliser.]"
    )
