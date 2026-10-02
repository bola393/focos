"""
Foco - aplicativo de foco (commitment device) para Windows.

Filosofia de design (leia antes de alterar):
    Este app foi feito para resistir ao IMPULSO de desistir, NAO ao dono da
    maquina. O usuario (que e o administrador) SEMPRE tem uma saida:

      1. Terminar a sessao digitando a frase-compromisso e esperando o atraso.
      2. O Gerenciador de Tarefas / Task Manager continua funcionando: encerrar
         o Foco.exe e o foco_watchdog e uma valvula de escape deliberada.
      3. Desinstalar normalmente (apagar a pasta) quando NAO houver sessao ativa.

    Propositalmente este programa NAO:
      - se instala como servico ou na inicializacao do Windows,
      - bloqueia o Gerenciador de Tarefas, regedit, ou ferramentas de admin,
      - sobrevive a um reboot alem da sessao,
      - tenta impedir a propria desinstalacao pelo administrador.

    Essas ausencias sao intencionais. Nao as remova: elas sao o que separa uma
    ferramenta de produtividade de um malware de persistencia.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import subprocess
import sys
import time

# tkinter e importado de forma preguicosa dentro da GUI: o modo --watchdog
# nao precisa de interface grafica e deve rodar sem essa dependencia.

# --------------------------------------------------------------------------- #
# Caminhos e constantes
# --------------------------------------------------------------------------- #

APP_NAME = "Foco"

# Diretorio de dados do usuario (nao requer admin para gravar).
_DATA_DIR = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")), "Foco"
)
os.makedirs(_DATA_DIR, exist_ok=True)

CONFIG_PATH = os.path.join(_DATA_DIR, "config.json")
SESSION_LOCK = os.path.join(_DATA_DIR, "session.lock")
LOG_PATH = os.path.join(_DATA_DIR, "sessoes.csv")

HOSTS_PATH = os.path.join(
    os.environ.get("SystemRoot", r"C:\Windows"),
    "System32", "drivers", "etc", "hosts",
)
HOSTS_MARK_BEGIN = "# >>> FOCO BLOCO INICIO >>>"
HOSTS_MARK_END = "# <<< FOCO BLOCO FIM <<<"

DEFAULT_CONFIG = {
    "focus_minutes": 25,
    "break_minutes": 5,
    "cycles": 4,
    "exit_delay_seconds": 120,
    # Frase longa de proposito: digitar da tempo para repensar o impulso.
    "passphrase": "eu escolhi focar agora e aceito esperar para sair",
    "auto_restart": True,
    "block_sites": True,
    "blocked_sites": [
        "facebook.com", "www.facebook.com",
        "instagram.com", "www.instagram.com",
        "twitter.com", "x.com", "www.twitter.com",
        "tiktok.com", "www.tiktok.com",
        "youtube.com", "www.youtube.com",
        "reddit.com", "www.reddit.com",
    ],
    "block_apps": False,
    # Cuidado: encerrar apps a forca pode causar perda de trabalho nao salvo.
    "blocked_apps": [],
}


# --------------------------------------------------------------------------- #
# Configuracao
# --------------------------------------------------------------------------- #

def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            user_cfg = json.load(fh)
        if isinstance(user_cfg, dict):
            cfg.update(user_cfg)
    except FileNotFoundError:
        save_config(cfg)
    except (json.JSONDecodeError, OSError):
        pass
    return cfg


def save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Estado da sessao (arquivo compartilhado com o watchdog)
# --------------------------------------------------------------------------- #

def write_session_lock(end_ts: float, main_pid: int) -> None:
    data = {"active": True, "end_ts": end_ts, "main_pid": main_pid}
    try:
        with open(SESSION_LOCK, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except OSError:
        pass


def read_session_lock() -> dict | None:
    try:
        with open(SESSION_LOCK, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def clear_session_lock() -> None:
    try:
        os.remove(SESSION_LOCK)
    except FileNotFoundError:
        pass
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Bloqueio de sites (hosts) e apps
# --------------------------------------------------------------------------- #

def _is_admin() -> bool:
    if os.name != "nt":
        return os.geteuid() == 0 if hasattr(os, "geteuid") else False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def apply_site_block(sites: list[str]) -> bool:
    """Adiciona entradas no hosts. Requer admin. Retorna True se aplicou."""
    if not sites:
        return False
    try:
        with open(HOSTS_PATH, "r", encoding="utf-8") as fh:
            content = fh.read()
    except OSError:
        return False

    # Remove bloco anterior, se houver, antes de reescrever.
    content = _strip_hosts_block(content)
    lines = [HOSTS_MARK_BEGIN]
    for site in sites:
        lines.append(f"127.0.0.1 {site}")
        lines.append(f"::1 {site}")
    lines.append(HOSTS_MARK_END)
    new_content = content.rstrip("\n") + "\n" + "\n".join(lines) + "\n"

    try:
        with open(HOSTS_PATH, "w", encoding="utf-8") as fh:
            fh.write(new_content)
        _flush_dns()
        return True
    except OSError:
        return False


def remove_site_block() -> None:
    try:
        with open(HOSTS_PATH, "r", encoding="utf-8") as fh:
            content = fh.read()
    except OSError:
        return
    new_content = _strip_hosts_block(content)
    if new_content != content:
        try:
            with open(HOSTS_PATH, "w", encoding="utf-8") as fh:
                fh.write(new_content)
            _flush_dns()
        except OSError:
            pass


def _strip_hosts_block(content: str) -> str:
    if HOSTS_MARK_BEGIN not in content:
        return content
    out, skipping = [], False
    for line in content.splitlines():
        if line.strip() == HOSTS_MARK_BEGIN:
            skipping = True
            continue
        if line.strip() == HOSTS_MARK_END:
            skipping = False
            continue
        if not skipping:
            out.append(line)
    return "\n".join(out) + "\n"


def _flush_dns() -> None:
    if os.name == "nt":
        try:
            subprocess.run(["ipconfig", "/flushdns"],
                           capture_output=True, timeout=10)
        except Exception:
            pass


def kill_blocked_apps(names: list[str]) -> None:
    if not names or os.name != "nt":
        return
    for name in names:
        try:
            subprocess.run(["taskkill", "/IM", name, "/F"],
                           capture_output=True, timeout=10)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Registro de sessoes
# --------------------------------------------------------------------------- #

def log_session(start: dt.datetime, end: dt.datetime, planned_min: int,
                completed: bool) -> None:
    new_file = not os.path.exists(LOG_PATH)
    try:
        with open(LOG_PATH, "a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new_file:
                w.writerow(["inicio", "fim", "minutos_planejados",
                            "minutos_reais", "concluida"])
            real_min = round((end - start).total_seconds() / 60, 1)
            w.writerow([start.isoformat(timespec="seconds"),
                        end.isoformat(timespec="seconds"),
                        planned_min, real_min,
                        "sim" if completed else "nao"])
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Watchdog: reinicia a janela de foco se ela for fechada durante a sessao.
# Projetado para ser ELE PROPRIO encerravel pelo dono (valvula de escape).
# --------------------------------------------------------------------------- #

def _self_launch_cmd(extra_args: list[str]) -> list[str]:
    if getattr(sys, "frozen", False):          # empacotado via PyInstaller
        return [sys.executable] + extra_args
    return [sys.executable, os.path.abspath(__file__)] + extra_args


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True, text=True, timeout=10,
            )
            return str(pid) in out.stdout
        except Exception:
            return True
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def run_watchdog() -> None:
    """Loop leve: enquanto a sessao estiver ativa, garante a janela aberta."""
    while True:
        lock = read_session_lock()
        if not lock or not lock.get("active"):
            return                                   # sessao terminou: sai
        if time.time() >= lock.get("end_ts", 0):
            return                                   # tempo acabou: sai
        main_pid = lock.get("main_pid")
        if main_pid and not _pid_alive(int(main_pid)):
            # Janela foi fechada antes da hora: reabre no modo retomar.
            try:
                proc = subprocess.Popen(_self_launch_cmd(["--resume"]))
                # Atualiza o PID no lock para o novo processo.
                lock["main_pid"] = proc.pid
                with open(SESSION_LOCK, "w", encoding="utf-8") as fh:
                    json.dump(lock, fh)
            except Exception:
                pass
        time.sleep(2)


def spawn_watchdog() -> None:
    try:
        flags = 0
        if os.name == "nt":
            flags = 0x08000000  # CREATE_NO_WINDOW
        subprocess.Popen(_self_launch_cmd(["--watchdog"]),
                         creationflags=flags)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Interface grafica
# --------------------------------------------------------------------------- #

class FocoApp:
    def __init__(self, root: tk.Tk, cfg: dict, resume: bool = False):
        self.root = root
        self.cfg = cfg
        self.running = False
        self.phase = "foco"          # "foco" ou "descanso"
        self.cycle = 1
        self.remaining = 0
        self.session_start: dt.datetime | None = None
        self.blocked_sites_applied = False

        root.title(f"{APP_NAME} - modo foco")
        root.configure(bg="#11131a")
        root.attributes("-topmost", True)
        root.protocol("WM_DELETE_WINDOW", self._on_close_request)

        self.timer_lbl = tk.Label(root, text="25:00", fg="#e8eaf0",
                                   bg="#11131a", font=("Segoe UI", 72, "bold"))
        self.timer_lbl.pack(pady=(40, 10), padx=60)

        self.phase_lbl = tk.Label(root, text="Pronto para focar",
                                  fg="#7aa2f7", bg="#11131a",
                                  font=("Segoe UI", 18))
        self.phase_lbl.pack(pady=(0, 6))

        self.cycle_lbl = tk.Label(root, text="", fg="#565f89",
                                  bg="#11131a", font=("Segoe UI", 12))
        self.cycle_lbl.pack(pady=(0, 24))

        self.start_btn = tk.Button(root, text="Iniciar foco",
                                   command=self.start_session,
                                   font=("Segoe UI", 14, "bold"),
                                   bg="#7aa2f7", fg="#11131a",
                                   activebackground="#9ab8ff",
                                   relief="flat", padx=24, pady=10)
        self.start_btn.pack(pady=(0, 10))

        self.quit_btn = tk.Button(root, text="Encerrar sessao",
                                  command=self.request_early_exit,
                                  font=("Segoe UI", 11),
                                  bg="#1a1d29", fg="#a9b1d6",
                                  activebackground="#2a2e3d",
                                  relief="flat", padx=16, pady=6)
        self.quit_btn.pack(pady=(0, 30))
        self.quit_btn.pack_forget()

        hint = ("Valvula de escape: o Gerenciador de Tarefas sempre funciona.\n"
                "Este app resiste ao impulso, nao a voce.")
        tk.Label(root, text=hint, fg="#3b4261", bg="#11131a",
                 font=("Segoe UI", 8)).pack(side="bottom", pady=8)

        root.update_idletasks()
        w, h = 560, 420
        x = (root.winfo_screenwidth() - w) // 2
        y = (root.winfo_screenheight() - h) // 2
        root.geometry(f"{w}x{h}+{x}+{y}")

        if resume:
            self._resume_from_lock()

    # ------------------------------------------------------------------ #

    def _resume_from_lock(self) -> None:
        lock = read_session_lock()
        if not lock or not lock.get("active"):
            return
        end_ts = lock.get("end_ts", 0)
        self.remaining = max(0, int(end_ts - time.time()))
        if self.remaining <= 0:
            clear_session_lock()
            return
        self.running = True
        self.phase = "foco"
        self.session_start = dt.datetime.now()
        self._enter_focus_state()
        self._tick()

    def _enter_focus_state(self) -> None:
        self.start_btn.pack_forget()
        self.quit_btn.pack(pady=(0, 30))
        self.root.attributes("-fullscreen", True)
        self._apply_blocks()

    def _apply_blocks(self) -> None:
        if self.cfg.get("block_sites") and not self.blocked_sites_applied:
            ok = apply_site_block(self.cfg.get("blocked_sites", []))
            self.blocked_sites_applied = ok
            if not ok and not _is_admin():
                self.phase_lbl.config(
                    text="Foco (sites nao bloqueados: rode como admin)")
        if self.cfg.get("block_apps"):
            kill_blocked_apps(self.cfg.get("blocked_apps", []))

    def _remove_blocks(self) -> None:
        if self.blocked_sites_applied:
            remove_site_block()
            self.blocked_sites_applied = False

    # ------------------------------------------------------------------ #

    def start_session(self) -> None:
        if self.running:
            return
        self.running = True
        self.phase = "foco"
        self.cycle = 1
        self.remaining = int(self.cfg.get("focus_minutes", 25)) * 60
        self.session_start = dt.datetime.now()
        total_planned = self._total_planned_seconds()
        write_session_lock(time.time() + total_planned, os.getpid())
        if self.cfg.get("auto_restart", True):
            spawn_watchdog()
        self._enter_focus_state()
        self._tick()

    def _total_planned_seconds(self) -> int:
        f = int(self.cfg.get("focus_minutes", 25)) * 60
        b = int(self.cfg.get("break_minutes", 5)) * 60
        c = max(1, int(self.cfg.get("cycles", 4)))
        return f * c + b * (c - 1)

    def _tick(self) -> None:
        if not self.running:
            return
        mins, secs = divmod(max(0, self.remaining), 60)
        self.timer_lbl.config(text=f"{mins:02d}:{secs:02d}")
        self.phase_lbl.config(
            text="Foco" if self.phase == "foco" else "Descanso")
        self.cycle_lbl.config(
            text=f"Ciclo {self.cycle} de {self.cfg.get('cycles', 4)}")

        if self.cfg.get("block_apps") and self.phase == "foco" \
                and self.remaining % 15 == 0:
            kill_blocked_apps(self.cfg.get("blocked_apps", []))

        if self.remaining <= 0:
            self._advance_phase()
            return
        self.remaining -= 1
        self.root.after(1000, self._tick)

    def _advance_phase(self) -> None:
        if self.phase == "foco":
            if self.cycle >= int(self.cfg.get("cycles", 4)):
                self._finish_session(completed=True)
                return
            self.phase = "descanso"
            self.remaining = int(self.cfg.get("break_minutes", 5)) * 60
            self._remove_blocks()
        else:
            self.phase = "foco"
            self.cycle += 1
            self.remaining = int(self.cfg.get("focus_minutes", 25)) * 60
            self._apply_blocks()
        self.root.after(1000, self._tick)

    # ------------------------------------------------------------------ #

    def _on_close_request(self) -> None:
        if self.running:
            # Durante a sessao, fechar a janela vira pedido de saida com atrito.
            self.request_early_exit()
        else:
            self.root.destroy()

    def request_early_exit(self) -> None:
        if not self.running:
            self.root.destroy()
            return
        phrase = self.cfg.get("passphrase", "")
        typed = self._ask_passphrase(phrase)
        if typed is None:
            return
        if typed.strip() != phrase.strip():
            messagebox.showwarning(
                APP_NAME, "Frase incorreta. A sessao continua.")
            return
        if not self._wait_exit_delay():
            return
        self._finish_session(completed=False)

    def _ask_passphrase(self, phrase: str) -> str | None:
        dlg = tk.Toplevel(self.root)
        dlg.title("Confirmar saida")
        dlg.configure(bg="#11131a")
        dlg.attributes("-topmost", True)
        dlg.grab_set()
        tk.Label(dlg, text="Para encerrar antes da hora, digite a frase:",
                 fg="#e8eaf0", bg="#11131a",
                 font=("Segoe UI", 11)).pack(padx=20, pady=(18, 6))
        tk.Label(dlg, text=phrase, fg="#7aa2f7", bg="#11131a",
                 wraplength=420, font=("Segoe UI", 11, "italic")
                 ).pack(padx=20, pady=(0, 12))
        entry = tk.Entry(dlg, width=52, font=("Segoe UI", 11))
        entry.pack(padx=20, pady=(0, 14))
        entry.focus_set()
        result = {"value": None}

        def ok():
            result["value"] = entry.get()
            dlg.destroy()

        def cancel():
            result["value"] = None
            dlg.destroy()

        btns = tk.Frame(dlg, bg="#11131a")
        btns.pack(pady=(0, 18))
        tk.Button(btns, text="Confirmar", command=ok, relief="flat",
                  bg="#7aa2f7", fg="#11131a", padx=16, pady=6).pack(
                      side="left", padx=6)
        tk.Button(btns, text="Voltar ao foco", command=cancel, relief="flat",
                  bg="#1a1d29", fg="#a9b1d6", padx=16, pady=6).pack(
                      side="left", padx=6)
        dlg.wait_window()
        return result["value"]

    def _wait_exit_delay(self) -> bool:
        delay = int(self.cfg.get("exit_delay_seconds", 120))
        if delay <= 0:
            return True
        dlg = tk.Toplevel(self.root)
        dlg.title("Aguardando")
        dlg.configure(bg="#11131a")
        dlg.attributes("-topmost", True)
        dlg.grab_set()
        lbl = tk.Label(dlg, fg="#e8eaf0", bg="#11131a",
                       font=("Segoe UI", 14))
        lbl.pack(padx=30, pady=20)
        info = tk.Label(dlg, text="Se mudar de ideia, volte a focar.",
                        fg="#565f89", bg="#11131a", font=("Segoe UI", 9))
        info.pack(pady=(0, 10))
        state = {"left": delay, "confirmed": None}

        def cancel():
            state["confirmed"] = False
            dlg.destroy()

        tk.Button(dlg, text="Cancelar e continuar focando", command=cancel,
                  relief="flat", bg="#1a1d29", fg="#a9b1d6",
                  padx=12, pady=6).pack(pady=(0, 16))

        def countdown():
            if state["confirmed"] is False:
                return
            if state["left"] <= 0:
                state["confirmed"] = True
                dlg.destroy()
                return
            m, s = divmod(state["left"], 60)
            lbl.config(text=f"Liberando em {m:02d}:{s:02d}")
            state["left"] -= 1
            dlg.after(1000, countdown)

        countdown()
        dlg.wait_window()
        return bool(state["confirmed"])

    # ------------------------------------------------------------------ #

    def _finish_session(self, completed: bool) -> None:
        self.running = False
        clear_session_lock()
        self._remove_blocks()
        if self.session_start:
            log_session(self.session_start, dt.datetime.now(),
                        int(self.cfg.get("focus_minutes", 25))
                        * int(self.cfg.get("cycles", 4)),
                        completed)
        try:
            self.root.attributes("-fullscreen", False)
        except tk.TclError:
            pass
        msg = ("Sessao concluida! Bom trabalho."
               if completed else "Sessao encerrada.")
        messagebox.showinfo(APP_NAME, msg)
        self.root.destroy()


# --------------------------------------------------------------------------- #
# Entrada
# --------------------------------------------------------------------------- #

def main() -> None:
    parser = argparse.ArgumentParser(description="Foco - app de foco")
    parser.add_argument("--watchdog", action="store_true",
                        help="modo interno: monitora a sessao")
    parser.add_argument("--resume", action="store_true",
                        help="modo interno: retoma uma sessao ativa")
    args = parser.parse_args()

    if args.watchdog:
        run_watchdog()
        return

    # Importa tkinter apenas no caminho da GUI (o watchdog nao precisa).
    import tkinter as tk
    from tkinter import messagebox
    globals()["tk"] = tk
    globals()["messagebox"] = messagebox

    cfg = load_config()
    root = tk.Tk()
    FocoApp(root, cfg, resume=args.resume)
    root.mainloop()


if __name__ == "__main__":
    main()
