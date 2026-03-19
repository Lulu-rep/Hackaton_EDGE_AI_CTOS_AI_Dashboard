import tkinter as tk
from tkinter import ttk, messagebox, font as tkfont
import serial
import serial.tools.list_ports
import threading
import queue
import json
import time
import re
from datetime import datetime
from collections import deque

# ─────────────────────────────────────────────
#  Constantes
# ─────────────────────────────────────────────
MATRIX_SIZE   = 8
N_PIXELS      = MATRIX_SIZE * MATRIX_SIZE
CELL_PX       = 52          # taille d'une cellule en pixels
DIST_MIN_MM   = 100
DIST_MAX_MM   = 400

# Palette distance : bleu (proche) → cyan → vert → jaune → rouge (loin)
PALETTE = [
    (0,   0,   255),
    (0,   128, 255),
    (0,   255, 255),
    (0,   255, 128),
    (0,   255,   0),
    (128, 255,   0),
    (255, 255,   0),
    (255, 128,   0),
    (255,  64,   0),
    (255,   0,   0),
]

def dist_to_color(dist_mm: int) -> str:
    """Convertit une distance en couleur hex RGB."""
    if dist_mm <= 0:
        return "#111827"
    ratio = min(dist_mm / DIST_MAX_MM, 1.0)
    idx   = ratio * (len(PALETTE) - 1)
    lo    = int(idx)
    hi    = min(lo + 1, len(PALETTE) - 1)
    t     = idx - lo
    r = int(PALETTE[lo][0] + t * (PALETTE[hi][0] - PALETTE[lo][0]))
    g = int(PALETTE[lo][1] + t * (PALETTE[hi][1] - PALETTE[lo][1]))
    b = int(PALETTE[lo][2] + t * (PALETTE[hi][2] - PALETTE[lo][2]))
    return f"#{r:02X}{g:02X}{b:02X}"

def luminance(hex_color: str) -> float:
    r = int(hex_color[1:3], 16) / 255
    g = int(hex_color[3:5], 16) / 255
    b = int(hex_color[5:7], 16) / 255
    return 0.299*r + 0.587*g + 0.114*b

# ─────────────────────────────────────────────
#  Parseur de trames
# ─────────────────────────────────────────────
class FrameParser:

    def __init__(self):
        self._buf   = b""

    def feed(self, data: bytes):
        frames = []
        self._buf += data

        while b'\n' in self._buf:
            line, self._buf = self._buf.split(b'\n', 1)
            try:
                text = line.decode("utf-8", errors="ignore").strip()
                text = re.sub(r'(\d)\s+(\d)', r'\1, \2', text)
                obj = json.loads(text)
                frame = self._parse_json(obj)
                if frame:
                    frames.append(frame)
            except Exception:
                pass

        return frames

    def _parse_json(self, obj: dict):
        matrix = obj.get("matrix", [])
        if len(matrix) == 0:
            return None
        if len(matrix) < N_PIXELS:
            return None
        return {
            "matrix":     [round(float(v)) for v in matrix[:N_PIXELS]],
            "ai_result":  bool(obj.get("ai_result", False)),
            "confidence": float(obj.get("confidence", 0.0)),
            "timestamp":  obj.get("ts", time.time()),
        }
    
# ─────────────────────────────────────────────
#  Application principale
# ─────────────────────────────────────────────
class ToFApp(tk.Tk):
    BG       = "#0D1117"
    PANEL    = "#161B22"
    BORDER   = "#21262D"
    ACCENT   = "#58A6FF"
    GREEN    = "#3FB950"
    RED      = "#F85149"
    ORANGE   = "#E3B341"
    TEXT     = "#C9D1D9"
    TEXT_DIM = "#484F58"
    WHITE    = "#F0F6FC"

    def __init__(self):
        super().__init__()
        self.title("CHALANT 9000 Ultra Pro Max")
        self.configure(bg=self.BG)
        self.resizable(True, True)
        self.minsize(980, 700)

        # État
        self._serial      = None
        self._thread      = None
        self._running     = False
        self._graph_w     = 0
        self._queue       = queue.Queue()
        self._parser      = FrameParser()
        self._fps_buf     = deque(maxlen=30)
        self._last_frame  = time.time()
        self._frame_count = 0
        self._history_ai  = deque(maxlen=50)
        self._conf_history= deque(maxlen=20)   

        # Données courantes
        self._matrix      = [0] * N_PIXELS
        self._ai_result   = False
        self._confidence  = 0.0
        self._show_values = tk.BooleanVar(value=True)
        self._show_grid   = tk.BooleanVar(value=True)
        self._dist_min_var= tk.IntVar(value=DIST_MIN_MM)
        self._dist_max_var= tk.IntVar(value=DIST_MAX_MM)

        self._build_ui()
        self._refresh_ports()
        self.after(50, self._poll_queue)

    # ── Construction UI ──────────────────────
    def _build_ui(self):
        self.columnconfigure(0, weight=0)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        self._build_sidebar()
        self._build_main()

    def _build_sidebar(self):
        side = tk.Frame(self, bg=self.PANEL, width=280)
        side.grid(row=0, column=0, sticky="nsew")
        side.grid_propagate(False)
        side.columnconfigure(0, weight=1)

        # Logo / titre
        title_f = tk.Frame(side, bg=self.PANEL, pady=18)
        title_f.grid(row=0, column=0, sticky="ew")
        tk.Label(title_f, text="CHALANT 9000", bg=self.PANEL,
                 fg=self.WHITE, font=("Courier New", 14, "bold")).pack()
        tk.Label(title_f, text="Ultra Pro Max", bg=self.PANEL,
                 fg=self.WHITE, font=("Courier New", 14, "bold")).pack()
        tk.Label(title_f, text="ST Edge AI · 8×8 Matrix", bg=self.PANEL,
                 fg=self.TEXT_DIM, font=("Courier New", 9)).pack()

        self._sep(side, 1)

        # ─ Section UART ─
        self._section(side, 2, "⚙  CONNEXION UART")

        uart_f = tk.Frame(side, bg=self.PANEL, padx=14)
        uart_f.grid(row=3, column=0, sticky="ew")
        uart_f.columnconfigure(1, weight=1)

        # Port
        tk.Label(uart_f, text="Port", bg=self.PANEL, fg=self.TEXT_DIM,
                 font=("Courier New", 9)).grid(row=0, column=0, sticky="w", pady=(6,2))
        self._port_var = tk.StringVar()
        self._port_cb  = ttk.Combobox(uart_f, textvariable=self._port_var,
                                       width=14, state="readonly")
        self._port_cb.grid(row=0, column=1, sticky="ew", padx=(6,0), pady=(6,2))

        # Refresh ports
        tk.Button(uart_f, text="↺", bg=self.BORDER, fg=self.ACCENT,
                  relief="flat", cursor="hand2", font=("Courier New", 12),
                  command=self._refresh_ports).grid(row=0, column=2, padx=(4,0), pady=(6,2))

        # Baudrate
        tk.Label(uart_f, text="Baud", bg=self.PANEL, fg=self.TEXT_DIM,
                 font=("Courier New", 9)).grid(row=1, column=0, sticky="w", pady=2)
        self._baud_var = tk.StringVar(value="115200")
        baud_cb = ttk.Combobox(uart_f, textvariable=self._baud_var, width=14, state="readonly",
                                values=["9600","19200","38400","57600","115200","230400","460800","921600"])
        baud_cb.grid(row=1, column=1, sticky="ew", padx=(6,0), pady=2)


        # Bouton Connect
        btn_f = tk.Frame(side, bg=self.PANEL, padx=14, pady=8)
        btn_f.grid(row=4, column=0, sticky="ew")
        btn_f.columnconfigure(0, weight=1)

        self._btn_connect = tk.Button(btn_f, text="CONNECTER", bg=self.ACCENT, fg="#000000",
                                       relief="flat", cursor="hand2", font=("Courier New", 9, "bold"),
                                       pady=6, command=self._toggle_connect)
        self._btn_connect.grid(row=0, column=0, sticky="ew")

        self._sep(side, 5)

        # ─ Section Affichage ─
        self._section(side, 6, "🎨  AFFICHAGE")

        disp_f = tk.Frame(side, bg=self.PANEL, padx=14)
        disp_f.grid(row=7, column=0, sticky="ew")
        disp_f.columnconfigure(1, weight=1)

        tk.Checkbutton(disp_f, text="Valeurs (mm)", variable=self._show_values,
                       bg=self.PANEL, fg=self.TEXT, selectcolor=self.BG,
                       activebackground=self.PANEL, activeforeground=self.ACCENT,
                       font=("Courier New", 9), command=self._redraw_matrix).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=2)

        tk.Checkbutton(disp_f, text="Grille", variable=self._show_grid,
                       bg=self.PANEL, fg=self.TEXT, selectcolor=self.BG,
                       activebackground=self.PANEL, activeforeground=self.ACCENT,
                       font=("Courier New", 9), command=self._redraw_matrix).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=2)

        tk.Label(disp_f, text="Dist. min (mm)", bg=self.PANEL, fg=self.TEXT_DIM,
                 font=("Courier New", 9)).grid(row=2, column=0, sticky="w", pady=(8,2))
        tk.Label(disp_f, text=f"{DIST_MIN_MM}", bg=self.PANEL, fg=self.ACCENT,
                 font=("Courier New", 9, "bold")).grid(row=2, column=1, sticky="e", pady=(8,2))

        tk.Label(disp_f, text="Dist. max (mm)", bg=self.PANEL, fg=self.TEXT_DIM,
                 font=("Courier New", 9)).grid(row=3, column=0, sticky="w", pady=2)
        tk.Label(disp_f, text=f"{DIST_MAX_MM}", bg=self.PANEL, fg=self.ACCENT,
                 font=("Courier New", 9, "bold")).grid(row=3, column=1, sticky="e", pady=2)

        self._sep(side, 8)

        # ─ Statistiques ─
        self._section(side, 9, "📊  STATISTIQUES")

        stats_f = tk.Frame(side, bg=self.PANEL, padx=14, pady=4)
        stats_f.grid(row=10, column=0, sticky="ew")
        stats_f.columnconfigure(1, weight=1)

        labels = [("FPS",       "_lbl_fps"),
                  ("Trames",    "_lbl_frames"),
                  ("Dist. min", "_lbl_dmin"),
                  ("Dist. max", "_lbl_dmax"),
                  ("Dist. moy", "_lbl_dmoy")]

        for i, (txt, attr) in enumerate(labels):
            tk.Label(stats_f, text=txt, bg=self.PANEL, fg=self.TEXT_DIM,
                     font=("Courier New", 9)).grid(row=i, column=0, sticky="w", pady=1)
            lbl = tk.Label(stats_f, text="—", bg=self.PANEL, fg=self.ACCENT,
                           font=("Courier New", 9, "bold"))
            lbl.grid(row=i, column=1, sticky="e", pady=1)
            setattr(self, attr, lbl)

        self._sep(side, 11)

        # ─ Log console ─
        self._section(side, 12, "📋  CONSOLE")
        log_f = tk.Frame(side, bg=self.PANEL, padx=14, pady=4)
        log_f.grid(row=13, column=0, sticky="nsew")
        side.rowconfigure(13, weight=1)
        self._log = tk.Text(log_f, bg=self.BG, fg=self.TEXT_DIM,
                             font=("Courier New", 8), relief="flat", height=8,
                             wrap="word", state="disabled", insertbackground=self.TEXT)
        sb = ttk.Scrollbar(log_f, command=self._log.yview)
        self._log.configure(yscrollcommand=sb.set)
        self._log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    def _build_main(self):
        main = tk.Frame(self, bg=self.BG)
        main.grid(row=0, column=1, sticky="nsew", padx=12, pady=12)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=0)
        main.rowconfigure(2, weight=1)

        # ── Bandeau IA ──────────────────────────
        self._ai_banner = tk.Frame(main, bg=self.BORDER, pady=10, padx=20,
                                    relief="flat", bd=0)
        self._ai_banner.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self._ai_banner.columnconfigure(1, weight=1)

        tk.Label(self._ai_banner, text="RÉSULTAT IA EDGE", bg=self.BORDER,
                 fg=self.TEXT_DIM, font=("Courier New", 9)).grid(row=0, column=0, sticky="w")

        self._ai_status_lbl = tk.Label(self._ai_banner, text="EN ATTENTE…",
                                        bg=self.BORDER, fg=self.TEXT_DIM,
                                        font=("Courier New", 18, "bold"))
        self._ai_status_lbl.grid(row=1, column=0, sticky="w")

        # indicateur LED
        self._led_canvas = tk.Canvas(self._ai_banner, width=24, height=24,
                                      bg=self.BORDER, highlightthickness=0)
        self._led_canvas.grid(row=0, column=2, rowspan=2, padx=(0,10))
        self._led_id = self._led_canvas.create_oval(2, 2, 22, 22, fill=self.TEXT_DIM, outline="")

        # Barre confiance
        conf_f = tk.Frame(self._ai_banner, bg=self.BORDER)
        conf_f.grid(row=0, column=3, rowspan=2, padx=(20, 0), sticky="e")
        tk.Label(conf_f, text="Confiance IA", bg=self.BORDER, fg=self.TEXT_DIM,
                 font=("Courier New", 8)).pack(anchor="w")
        self._conf_bar_bg = tk.Canvas(conf_f, width=160, height=16,
                                       bg=self.BG, highlightthickness=0)
        self._conf_bar_bg.pack()
        self._conf_bar_rect = self._conf_bar_bg.create_rectangle(0, 0, 0, 16,
                                                                   fill=self.ACCENT, outline="")
        self._conf_pct_lbl  = tk.Label(conf_f, text="0 %", bg=self.BORDER,
                                        fg=self.ACCENT, font=("Courier New", 9, "bold"))
        self._conf_pct_lbl.pack(anchor="e")

        # ── Matrice ToF ──────────────────────────
        mat_outer = tk.Frame(main, bg=self.BG)
        mat_outer.grid(row=1, column=0, sticky="n")

        tk.Label(mat_outer, text="CARTE DE DISTANCE — MATRICE 8×8",
                 bg=self.BG, fg=self.TEXT_DIM, font=("Courier New", 9)).pack(anchor="w", pady=(0,4))

        canvas_size = CELL_PX * MATRIX_SIZE
        self._canvas = tk.Canvas(mat_outer, width=canvas_size, height=canvas_size,
                                  bg=self.BG, highlightthickness=2,
                                  highlightbackground=self.BORDER)
        self._canvas.pack()

        # Légende couleur dynamique
        leg_f = tk.Frame(mat_outer, bg=self.BG)
        leg_f.pack(fill="x", pady=(4, 0))
        self._leg_min_lbl = tk.Label(leg_f, text="— mm", bg=self.BG, fg=self.TEXT_DIM,
                                      font=("Courier New", 8))
        self._leg_min_lbl.pack(side="left")
        self._leg_cv = tk.Canvas(leg_f, width=200, height=10, bg=self.BG, highlightthickness=0)
        self._leg_cv.pack(side="left", padx=6)
        self._leg_max_lbl = tk.Label(leg_f, text="— mm", bg=self.BG, fg=self.TEXT_DIM,
                                      font=("Courier New", 8))
        self._leg_max_lbl.pack(side="left")


        # ── Graphe historique ──────────────────
        graph_frame = tk.Frame(main, bg=self.PANEL, pady=8, padx=10)
        graph_frame.grid(row=2, column=0, sticky="nsew", pady=(10, 0))
        main.rowconfigure(2, weight=1)
        graph_frame.columnconfigure(0, weight=1)
        graph_frame.rowconfigure(1, weight=1)

        tk.Label(graph_frame, text="HISTORIQUE — CONFIANCE IA (%)",
                 bg=self.PANEL, fg=self.TEXT_DIM, font=("Courier New", 9)).grid(
            row=0, column=0, sticky="w", pady=(0,4))

        self._graph = tk.Canvas(graph_frame, bg=self.BG, highlightthickness=0, height=100)
        self._graph.grid(row=1, column=0, sticky="nsew")
        self._graph.bind("<Configure>", lambda e: setattr(self, "_graph_w", e.width))

        # Pré-dessin matrice vide
        self._cell_ids    = []
        self._value_ids   = []
        self._draw_matrix_skeleton()

    # ── Helpers UI ───────────────────────────
    def _sep(self, parent, row):
        tk.Frame(parent, bg=self.BORDER, height=1).grid(
            row=row, column=0, sticky="ew", padx=8, pady=4)

    def _section(self, parent, row, title):
        tk.Label(parent, text=title, bg=self.PANEL, fg=self.TEXT_DIM,
                 font=("Courier New", 9), anchor="w", padx=14, pady=6).grid(
            row=row, column=0, sticky="ew")

    def _log_msg(self, msg: str, level="info"):
        colors = {"info": self.TEXT_DIM, "ok": self.GREEN,
                  "warn": self.ORANGE, "err": self.RED}
        ts  = datetime.now().strftime("%H:%M:%S")
        tag = f"t_{int(time.time()*1000)}"
        self._log.configure(state="normal")
        self._log.insert("end", f"[{ts}] {msg}\n", tag)
        self._log.tag_configure(tag, foreground=colors.get(level, self.TEXT_DIM))
        self._log.configure(state="disabled")
        self._log.see("end")

    # ── Matrice canvas ────────────────────────
    def _draw_matrix_skeleton(self):
        """Crée une fois pour toutes les rectangles et labels."""
        self._canvas.delete("all")
        self._cell_ids  = []
        self._value_ids = []
        cs = CELL_PX
        for i in range(N_PIXELS):
            r, c = divmod(i, MATRIX_SIZE)
            x0, y0 = c * cs, r * cs
            rid = self._canvas.create_rectangle(x0, y0, x0+cs, y0+cs,
                                                 fill="#111827", outline="")
            self._cell_ids.append(rid)
            vid = self._canvas.create_text(x0 + cs//2, y0 + cs//2,
                                            text="", fill="white",
                                            font=("Courier New", 8, "bold"))
            self._value_ids.append(vid)

    def _redraw_matrix(self):
        cs    = CELL_PX
        show  = self._show_values.get()
        grid  = self._show_grid.get()

        # Échelle dynamique sur les pixels valides de la frame courante
        valid = [d for d in self._matrix if d > 0]
        dmin  = min(valid) if valid else 0
        dmax  = max(valid) if valid else 1
        scale = dmax - dmin or 1

        # Supprimer ancienne grille
        self._canvas.delete("grid_line")

        for i, dist in enumerate(self._matrix):
            r, c = divmod(i, MATRIX_SIZE)
            if dist <= 0:
                color = "#111827"
            else:
                # norm : 0.0 (plus proche) → 1.0 (plus loin)
                # On passe max(1, ...) pour éviter le cas dist_mm=0 → noir
                norm  = (dist - dmin) / scale
                color = dist_to_color(max(1, int(norm * DIST_MAX_MM)))
            self._canvas.itemconfig(self._cell_ids[i], fill=color)
            lum   = luminance(color)
            fg    = "#000000" if lum > 0.55 else "#FFFFFF"
            txt   = str(dist) if (show and dist > 0) else ""
            self._canvas.itemconfig(self._value_ids[i], text=txt, fill=fg)

        # Mise à jour légende
        self._leg_min_lbl.configure(text=f"{dmin} mm")
        self._leg_max_lbl.configure(text=f"{dmax} mm")
        self._leg_cv.delete("all")
        for i in range(200):
            c = dist_to_color(max(1, int((i / 200) * DIST_MAX_MM)))
            self._leg_cv.create_rectangle(i, 0, i + 1, 10, fill=c, outline="")

        if grid:
            total = CELL_PX * MATRIX_SIZE
            for k in range(1, MATRIX_SIZE):
                pos = k * CELL_PX
                self._canvas.create_line(pos, 0, pos, total, fill="#1C2333",
                                          width=1, tags="grid_line")
                self._canvas.create_line(0, pos, total, pos, fill="#1C2333",
                                          width=1, tags="grid_line")

    # ── Graphe ───────────────────────────────
    def _redraw_graph(self):
        self._graph.delete("all")
        w = self._graph_w if self._graph_w > 10 else self._graph.winfo_width()
        h = self._graph.winfo_height()
        if w < 10 or h < 10 or len(self._conf_history) < 2:
            return

        data = list(self._conf_history)   # liste de (timestamp, confidence)
        t0   = data[0][0]
        t1   = data[-1][0]
        span = t1 - t0 or 1.0

        # X proportionnel au temps, Y = confiance 0-100%
        pts = []
        for ts, conf in data:
            x = 1 + int((ts - t0) / span * (w - 3))
            y = h - 2 - int((conf / 100.0) * (h - 4))
            pts.append((x, y))

        # Segments colorés ancien->récent (bleu->orange)
        for i in range(len(pts) - 1):
            t = i / max(len(pts) - 1, 1)
            r = int(88  + t * (248 - 88))
            g = int(166 - t * (166 - 81))
            b = int(255 - t * (255 - 73))
            self._graph.create_line(pts[i][0], pts[i][1], pts[i+1][0], pts[i+1][1],
                                     fill=f"#{r:02X}{g:02X}{b:02X}", width=2)

        # Axes
        self._graph.create_line(1, 0, 1, h - 1, fill=self.BORDER, width=1)
        self._graph.create_line(0, h - 2, w, h - 2, fill=self.BORDER, width=1)

        # Labels
        self._graph.create_text(4, 3,        text="100%", anchor="nw",
                                 fill=self.TEXT_DIM, font=("Courier New", 7))
        self._graph.create_text(4, h - 14,   text="0%",   anchor="nw",
                                 fill=self.TEXT_DIM, font=("Courier New", 7))
        self._graph.create_text(w - 2, h - 14, text=f"{span:.1f}s", anchor="ne",
                                 fill=self.TEXT_DIM, font=("Courier New", 7))

        # Marqueurs IA
        ai_data = list(self._history_ai)
        offset  = len(data) - len(ai_data)
        for i, val in enumerate(ai_data):
            idx = offset + i
            if idx < 0 or idx >= len(pts):
                continue
            x, y  = pts[idx]
            color = self.GREEN if val else self.RED
            self._graph.create_oval(x - 3, y - 3, x + 3, y + 3, fill=color, outline="")

    # ── Mise à jour UI depuis une trame ──────
    def _apply_frame(self, frame: dict):
        self._matrix      = frame["matrix"]
        self._ai_result   = frame["ai_result"]
        self._confidence  = frame["confidence"]

        # stats timing
        now = time.time()
        dt  = now - self._last_frame
        self._last_frame = now
        self._fps_buf.append(1.0 / max(dt, 0.001))
        self._frame_count += 1

        # historiques
        valid = [d for d in self._matrix if d > 0]
        dmin  = min(valid) if valid else 0
        self._conf_history.append((now, self._confidence))
        self._history_ai.append(self._ai_result)

        # ── Matrice ──
        self._redraw_matrix()

        # ── Bandeau IA ──
        if self._ai_result:
            color = self.GREEN
            txt   = "✔  OBJET CONFORME"
        else:
            color = self.RED
            txt   = "✘  NON CONFORME"

        self._ai_banner.configure(bg=self._darken(color))
        for w in self._ai_banner.winfo_children():
            try:
                w.configure(bg=self._darken(color))
            except Exception:
                pass
        self._ai_status_lbl.configure(text=txt, fg=color)
        self._led_canvas.itemconfig(self._led_id, fill=color)
        self._led_canvas.configure(bg=self._darken(color))

        # barre confiance
        pct = int(self._confidence)
        w   = int(self._confidence / 100 * 160)
        conf_color = self.GREEN if pct >= 70 else (self.ORANGE if pct >= 40 else self.RED)
        self._conf_bar_bg.coords(self._conf_bar_rect, 0, 0, w, 16)
        self._conf_bar_bg.itemconfig(self._conf_bar_rect, fill=conf_color)
        self._conf_pct_lbl.configure(text=f"{pct} %", fg=conf_color)

        # ── Stats ──
        fps  = sum(self._fps_buf) / len(self._fps_buf) if self._fps_buf else 0
        dmax = max(valid) if valid else 0
        dmoy = int(sum(valid) / len(valid)) if valid else 0
        self._lbl_fps.config(text=f"{fps:.1f}")
        self._lbl_frames.config(text=str(self._frame_count))
        self._lbl_dmin.config(text=f"{dmin} mm")
        self._lbl_dmax.config(text=f"{dmax} mm")
        self._lbl_dmoy.config(text=f"{dmoy} mm")

        # ── Graphe ──
        self._redraw_graph()

    @staticmethod
    def _darken(hex_color: str, factor=0.15) -> str:
        r = int(int(hex_color[1:3], 16) * factor)
        g = int(int(hex_color[3:5], 16) * factor)
        b = int(int(hex_color[5:7], 16) * factor)
        return f"#{r:02X}{g:02X}{b:02X}"

    # ── Polling queue ─────────────────────────
    def _poll_queue(self):
        try:
            while True:
                frame = self._queue.get_nowait()
                self._apply_frame(frame)
        except queue.Empty:
            pass
        self.after(30, self._poll_queue)

    # ── Thread UART ───────────────────────────
    def _uart_thread(self):
        port = self._port_var.get()
        baud = self._baud_var.get()
        self.after(0, lambda: self._log_msg(f"Connexion {port} @ {baud} baud…", "info"))
        try:
            ser = serial.Serial(
                port=port,
                baudrate=int(baud),
                timeout=0.1
            )
            self._serial = ser
            self.after(0, lambda: self._log_msg("Port ouvert ✔", "ok"))
            while self._running:
                waiting = ser.in_waiting
                raw = ser.read(waiting if waiting > 0 else 1)
                if raw:
                    frames = self._parser.feed(raw)
                    for f in frames:
                        self._queue.put(f)
        except serial.SerialException as e:
            msg = str(e)
            self.after(0, lambda: self._log_msg(f"Erreur série : {msg}", "err"))
        finally:
            if self._serial and self._serial.is_open:
                self._serial.close()
            self._serial = None
            self._running = False
            self.after(0, self._on_disconnect)

    # ── Connexion / Déconnexion ───────────────
    def _toggle_connect(self):
        if self._running:
            self._running = False
            self._log_msg("Déconnecté", "warn")
            self._on_disconnect()
        else:
            port = self._port_var.get()
            if not port:
                messagebox.showwarning("Port manquant", "Sélectionnez un port COM/tty.")
                return
            self._running = True
            self._conf_history.clear()
            self._history_ai.clear()
            self._parser  = FrameParser()
            self._thread  = threading.Thread(target=self._uart_thread, daemon=True)
            self._thread.start()
            self._btn_connect.configure(text="DÉCONNECTER", bg=self.RED)

    def _on_disconnect(self):
        self._btn_connect.configure(text="CONNECTER", bg=self.ACCENT, fg="#000", state="normal")

    def _refresh_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self._port_cb["values"] = ports
        if ports:
            self._port_var.set(ports[0])
        self._log_msg(f"{len(ports)} port(s) détecté(s) : {', '.join(ports) or 'aucun'}")

        self._parser = FrameParser()


# ─────────────────────────────────────────────
#  Style ttk
# ─────────────────────────────────────────────
def apply_dark_style():
    style = ttk.Style()
    style.theme_use("default")
    bg, fg, sel = "#161B22", "#C9D1D9", "#58A6FF"
    style.configure("TCombobox",
                    fieldbackground=bg, background=bg, foreground=fg,
                    selectbackground=sel, selectforeground="#000",
                    arrowcolor=sel, borderwidth=0)
    style.map("TCombobox", fieldbackground=[("readonly", bg)],
              foreground=[("readonly", fg)])
    style.configure("TScrollbar", background="#21262D", troughcolor="#0D1117",
                    arrowcolor="#484F58", borderwidth=0)


# ─────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────
if __name__ == "__main__":
    app = ToFApp()
    apply_dark_style()
    app.mainloop()