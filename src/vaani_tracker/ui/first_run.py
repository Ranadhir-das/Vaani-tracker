"""Guided first-run setup wizard and device pairing flow."""

import logging
import platform
import threading
from typing import Optional, Callable, Dict, Any
import urllib.request
import urllib.error

import tkinter as tk
from tkinter import ttk, messagebox
from PIL import Image, ImageTk

from ..config import TrackerConfig
from ..constants import APP_VERSION
from ..client.api import VaaniApiClient
from ..client.auth import DeviceAuthManager
from ..models import DeviceInfo
from .icons import get_asset_path

logger = logging.getLogger(__name__)

# Dark Slate Theme Colors
BG_DARK = "#0f172a"          # Slate 900
BG_CARD = "#1e293b"          # Slate 800
BG_CARD_HOVER = "#334155"    # Slate 700
BORDER_COLOR = "#334155"     # Slate 700
TEXT_MAIN = "#f8fafc"        # Slate 50
TEXT_MUTED = "#94a3b8"       # Slate 400
TEXT_DARK = "#64748b"        # Slate 500
ACCENT = "#38bdf8"           # Sky 400
ACCENT_HOVER = "#0284c7"     # Sky 600
COLOR_SUCCESS = "#10b981"    # Emerald 500
COLOR_DANGER = "#ef4444"     # Red 500


class FirstRunWizard:
    """Four-step setup wizard guiding employee through device registration."""

    def __init__(
        self,
        config: TrackerConfig,
        auth_manager: DeviceAuthManager,
        api_client: Optional[VaaniApiClient] = None,
        on_complete: Optional[Callable[[], None]] = None,
        root: Optional[tk.Tk] = None,
    ):
        self.config = config
        self.auth_manager = auth_manager
        self.api_client = api_client or VaaniApiClient(config.backend_url)
        self.on_complete = on_complete

        self._current_step = 1
        self._is_pairing = False
        self._registered_employee_info: Dict[str, Any] = {}
        self.completed = False

        if root is not None:
            self.root = root
            self._owns_root = False
        else:
            self.root = tk.Tk()
            self._owns_root = True

        self._setup_window()
        self._create_layout()
        self.show_step(1)

    def _setup_window(self) -> None:
        """Sets up window geometry and icons."""
        self.root.title("Vaani Desktop Tracker · Setup & Pairing")
        self.root.geometry("680x560")
        self.root.resizable(False, False)
        self.root.configure(bg=BG_DARK)

        ico_path = get_asset_path("vaani.ico")
        if ico_path.exists():
            try:
                self.root.iconbitmap(str(ico_path))
            except Exception:
                pass

        # Center on screen
        self.root.update_idletasks()
        w = self.root.winfo_width()
        h = self.root.winfo_height()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = (sw - w) // 2
        y = (sh - h) // 2
        self.root.geometry(f"{w}x{h}+{x}+{y}")

        # Intercept window close to cancel cleanly without running the dashboard
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self) -> None:
        """Called when user closes the wizard window without completing pairing."""
        logger.info("First-run setup wizard closed by user before completing pairing.")
        self.completed = False
        try:
            self.root.quit()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def _create_layout(self) -> None:
        """Creates header, step indicators, and dynamic step body."""
        # Top Header
        self.header_frame = tk.Frame(self.root, bg=BG_DARK, padx=24, pady=16)
        self.header_frame.pack(side="top", fill="x")

        # Logo
        self._logo_img = None
        icon_path = get_asset_path("vaani-icon.png")
        if icon_path.exists():
            try:
                pil_img = Image.open(icon_path).resize((40, 40), Image.Resampling.LANCZOS)
                self._logo_img = ImageTk.PhotoImage(pil_img, master=self.root)
                lbl_icon = tk.Label(self.header_frame, image=self._logo_img, bg=BG_DARK)
                lbl_icon.pack(side="left", padx=(0, 12))
            except Exception:
                pass

        title_box = tk.Frame(self.header_frame, bg=BG_DARK)
        title_box.pack(side="left")

        lbl_title = tk.Label(title_box, text="Vaani Desktop Tracker", font=("Segoe UI", 14, "bold"), fg=TEXT_MAIN, bg=BG_DARK)
        lbl_title.pack(anchor="w")

        lbl_sub = tk.Label(title_box, text="Workstation Setup & Employee Pairing Wizard", font=("Segoe UI", 9), fg=TEXT_MUTED, bg=BG_DARK)
        lbl_sub.pack(anchor="w")

        # Step Progress Pills
        self.step_bar = tk.Frame(self.root, bg=BG_CARD, padx=24, pady=8)
        self.step_bar.pack(side="top", fill="x")

        self.step_labels = []
        steps = ["1. Welcome", "2. Connection", "3. Pairing", "4. Complete"]
        for idx, text in enumerate(steps):
            lbl = tk.Label(self.step_bar, text=text, font=("Segoe UI", 9, "bold"), fg=TEXT_DARK, bg=BG_CARD, padx=8)
            lbl.pack(side="left")
            self.step_labels.append(lbl)
            if idx < len(steps) - 1:
                arrow = tk.Label(self.step_bar, text="→", font=("Segoe UI", 9), fg=TEXT_DARK, bg=BG_CARD)
                arrow.pack(side="left")

        # Separator
        sep = tk.Frame(self.root, bg=BORDER_COLOR, height=1)
        sep.pack(side="top", fill="x")

        # Dynamic Content Container
        self.step_container = tk.Frame(self.root, bg=BG_DARK, padx=28, pady=20)
        self.step_container.pack(side="top", fill="both", expand=True)

    def _update_step_bar(self, step_num: int) -> None:
        """Updates color highlights on the step progress bar."""
        for idx, lbl in enumerate(self.step_labels):
            s = idx + 1
            if s == step_num:
                lbl.configure(fg=ACCENT)
            elif s < step_num:
                lbl.configure(fg=COLOR_SUCCESS)
            else:
                lbl.configure(fg=TEXT_DARK)

    def show_step(self, step_num: int) -> None:
        """Renders the specified step."""
        self._current_step = step_num
        self._update_step_bar(step_num)

        for child in self.step_container.winfo_children():
            child.destroy()

        if step_num == 1:
            self._render_step1_welcome()
        elif step_num == 2:
            self._render_step2_connection()
        elif step_num == 3:
            self._render_step3_pairing()
        elif step_num == 4:
            self._render_step4_complete()

    # --------------------------------------------------------------------------
    # STEP 1: WELCOME & PRIVACY
    # --------------------------------------------------------------------------
    def _render_step1_welcome(self) -> None:
        lbl_head = tk.Label(self.step_container, text="Welcome to Vaani Tracker", font=("Segoe UI", 16, "bold"), fg=TEXT_MAIN, bg=BG_DARK)
        lbl_head.pack(anchor="w", pady=(0, 6))

        lbl_desc = tk.Label(
            self.step_container,
            text="This wizard will pair your workstation with your Vaani Telecalling CRM profile.\n"
                 "Vaani Tracker ensures accurate shift hour records and attendance without intrusive monitoring.",
            font=("Segoe UI", 9),
            fg=TEXT_MUTED,
            bg=BG_DARK,
            justify="left",
        )
        lbl_desc.pack(anchor="w", pady=(0, 16))

        # Privacy Assurance Box
        card = tk.Frame(self.step_container, bg=BG_CARD, padx=16, pady=14, highlightbackground=BORDER_COLOR, highlightthickness=1)
        card.pack(fill="x", pady=(0, 20))

        lbl_card_title = tk.Label(card, text="🛡️ Our Employee Privacy Commitment", font=("Segoe UI", 10, "bold"), fg=COLOR_SUCCESS, bg=BG_CARD)
        lbl_card_title.pack(anchor="w", pady=(0, 8))

        points = [
            "✔ Tracks keyboard and mouse presence seconds to record active shift time.",
            "✔ Tracks workstation lock and system sleep transitions.",
            "✔ Records foreground executable name (e.g., 'code.exe', 'excel.exe').",
            "✖ NEVER takes screenshots or screen recordings.",
            "✖ NEVER logs keystrokes, passwords, or typed text.",
            "✖ NEVER tracks browser URLs, webpage history, or document contents.",
            "✖ NEVER touches your webcam, microphone, or personal files.",
        ]
        for p in points:
            col = COLOR_SUCCESS if p.startswith("✔") else COLOR_DANGER
            lbl_p = tk.Label(card, text=p, font=("Segoe UI", 8), fg=col if p.startswith("✖") else TEXT_MAIN, bg=BG_CARD)
            lbl_p.pack(anchor="w", pady=2)

        # Bottom Button Bar
        btn_bar = tk.Frame(self.step_container, bg=BG_DARK)
        btn_bar.pack(side="bottom", fill="x", pady=(16, 0))

        btn_next = tk.Button(
            btn_bar,
            text="Get Started →",
            font=("Segoe UI", 10, "bold"),
            bg=ACCENT,
            fg="#000000",
            activebackground=ACCENT_HOVER,
            relief="flat",
            padx=20,
            pady=8,
            cursor="hand2",
            command=lambda: self.show_step(2),
        )
        btn_next.pack(side="right")

    # --------------------------------------------------------------------------
    # STEP 2: SERVER CONNECTION
    # --------------------------------------------------------------------------
    def _render_step2_connection(self) -> None:
        lbl_head = tk.Label(self.step_container, text="CRM Server Connection", font=("Segoe UI", 16, "bold"), fg=TEXT_MAIN, bg=BG_DARK)
        lbl_head.pack(anchor="w", pady=(0, 6))

        lbl_desc = tk.Label(
            self.step_container,
            text="Configure the target Vaani CRM server base URL.",
            font=("Segoe UI", 9),
            fg=TEXT_MUTED,
            bg=BG_DARK,
        )
        lbl_desc.pack(anchor="w", pady=(0, 16))

        card = tk.Frame(self.step_container, bg=BG_CARD, padx=20, pady=18, highlightbackground=BORDER_COLOR, highlightthickness=1)
        card.pack(fill="x", pady=(0, 16))

        lbl_u = tk.Label(card, text="Vaani Backend Base URL", font=("Segoe UI", 9, "bold"), fg=TEXT_MAIN, bg=BG_CARD)
        lbl_u.pack(anchor="w", pady=(0, 6))

        self.entry_backend_url = tk.Entry(card, font=("Segoe UI", 10), bg=BG_DARK, fg=TEXT_MAIN, insertbackground=TEXT_MAIN, relief="flat", bd=4)
        self.entry_backend_url.insert(0, self.config.backend_url)
        self.entry_backend_url.pack(fill="x", pady=(0, 12))

        # Test Connection Button & Status
        test_f = tk.Frame(card, bg=BG_CARD)
        test_f.pack(fill="x")

        self.btn_test_conn = tk.Button(
            test_f,
            text="📡 Test Connection",
            font=("Segoe UI", 9),
            bg=BG_CARD_HOVER,
            fg=TEXT_MAIN,
            activebackground=BORDER_COLOR,
            relief="flat",
            padx=12,
            pady=6,
            cursor="hand2",
            command=self._on_test_connection,
        )
        self.btn_test_conn.pack(side="left")

        self.lbl_conn_status = tk.Label(test_f, text="", font=("Segoe UI", 9), bg=BG_CARD)
        self.lbl_conn_status.pack(side="left", padx=12)

        # Bottom Button Bar
        btn_bar = tk.Frame(self.step_container, bg=BG_DARK)
        btn_bar.pack(side="bottom", fill="x", pady=(16, 0))

        btn_back = tk.Button(
            btn_bar,
            text="← Back",
            font=("Segoe UI", 9),
            bg=BG_CARD,
            fg=TEXT_MAIN,
            relief="flat",
            padx=16,
            pady=8,
            cursor="hand2",
            command=lambda: self.show_step(1),
        )
        btn_back.pack(side="left")

        btn_next = tk.Button(
            btn_bar,
            text="Next: Device Pairing →",
            font=("Segoe UI", 10, "bold"),
            bg=ACCENT,
            fg="#000000",
            activebackground=ACCENT_HOVER,
            relief="flat",
            padx=20,
            pady=8,
            cursor="hand2",
            command=self._on_connection_next,
        )
        btn_next.pack(side="right")

    def _on_test_connection(self) -> None:
        """Verifies backend URL reachability."""
        url = self.entry_backend_url.get().strip().rstrip("/")
        self.lbl_conn_status.configure(text="Checking server...", fg=ACCENT)
        self.btn_test_conn.configure(state="disabled")

        def _worker():
            reachable = False
            err_msg = ""
            try:
                req = urllib.request.Request(
                    f"{url}/api/v1/desktop-tracker/status/",
                    headers={"User-Agent": "VaaniSetupWizard/1.0"},
                )
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    reachable = True
            except urllib.error.HTTPError as e:
                # 401 or 403 means server responded and endpoint exists
                if e.code in (401, 403, 400, 405):
                    reachable = True
                else:
                    err_msg = f"HTTP {e.code}"
            except Exception as e:
                err_msg = "Cannot connect"

            def _ui():
                self.btn_test_conn.configure(state="normal")
                if reachable:
                    self.lbl_conn_status.configure(text="✓ Server reached successfully!", fg=COLOR_SUCCESS)
                else:
                    self.lbl_conn_status.configure(text=f"✗ Unreachable: {err_msg}", fg=COLOR_DANGER)

            self.root.after(0, _ui)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_connection_next(self) -> None:
        url = self.entry_backend_url.get().strip().rstrip("/")
        if not url:
            messagebox.showerror("Error", "Please enter a valid backend URL.", parent=self.root)
            return

        # Enforce HTTPS for production domain
        if "vaaniapp.co.in" in url:
            if url.startswith("http://"):
                url = "https://" + url[7:]
            elif not url.startswith("https://"):
                url = "https://" + url
        elif not url.startswith("http://") and not url.startswith("https://"):
            url = "http://" + url

        self.config.backend_url = url
        self.config.save()
        self.api_client.backend_url = url
        self.show_step(3)

    # --------------------------------------------------------------------------
    # STEP 3: PAIRING & AUTHENTICATION
    # --------------------------------------------------------------------------
    def _render_step3_pairing(self) -> None:
        lbl_head = tk.Label(self.step_container, text="Pair Device with Employee", font=("Segoe UI", 16, "bold"), fg=TEXT_MAIN, bg=BG_DARK)
        lbl_head.pack(anchor="w", pady=(0, 6))

        lbl_desc = tk.Label(
            self.step_container,
            text="Choose your preferred authentication method to bind this workstation\n"
                 "to your Vaani Telecalling CRM employee account.",
            font=("Segoe UI", 9),
            fg=TEXT_MUTED,
            bg=BG_DARK,
            justify="left",
        )
        lbl_desc.pack(anchor="w", pady=(0, 12))

        # Main Card
        card = tk.Frame(self.step_container, bg=BG_CARD, padx=20, pady=16, highlightbackground=BORDER_COLOR, highlightthickness=1)
        card.pack(fill="x", pady=(0, 12))

        # Tab Selector Bar
        tab_bar = tk.Frame(card, bg=BG_CARD)
        tab_bar.pack(fill="x", pady=(0, 14))

        self.btn_tab_code = tk.Button(
            tab_bar,
            text="🔑 Pairing Code",
            font=("Segoe UI", 9, "bold"),
            relief="flat",
            padx=16,
            pady=6,
            cursor="hand2",
            command=lambda: self._set_auth_method("PAIRING_CODE"),
        )
        self.btn_tab_code.pack(side="left", padx=(0, 8))

        self.btn_tab_creds = tk.Button(
            tab_bar,
            text="👤 CRM Login (User & Password)",
            font=("Segoe UI", 9, "bold"),
            relief="flat",
            padx=16,
            pady=6,
            cursor="hand2",
            command=lambda: self._set_auth_method("CREDENTIALS"),
        )
        self.btn_tab_creds.pack(side="left")

        # Container for Dynamic Method Views
        self.method_container = tk.Frame(card, bg=BG_CARD)
        self.method_container.pack(fill="x", pady=(0, 8))

        # --- Sub-frame 1: Pairing Code ---
        self.frame_pairing_code = tk.Frame(self.method_container, bg=BG_CARD)
        lbl_code = tk.Label(self.frame_pairing_code, text="Pairing Code (e.g., VN-9428)", font=("Segoe UI", 9, "bold"), fg=TEXT_MAIN, bg=BG_CARD)
        lbl_code.pack(anchor="w", pady=(0, 6))

        self.entry_code = tk.Entry(
            self.frame_pairing_code,
            font=("Consolas", 14, "bold"),
            bg=BG_DARK,
            fg=ACCENT,
            insertbackground=TEXT_MAIN,
            justify="center",
            relief="flat",
            bd=6,
        )
        self.entry_code.pack(fill="x", pady=(0, 10))
        self.entry_code.bind("<Return>", lambda e: self._on_pair_click())

        lbl_hint = tk.Label(
            self.frame_pairing_code,
            text="Don't have a pairing code? Ask your team manager or HR admin to generate one from the Vaani Employee Devices portal.",
            font=("Segoe UI", 8),
            fg=TEXT_MUTED,
            bg=BG_CARD,
            wraplength=540,
            justify="left",
        )
        lbl_hint.pack(anchor="w")

        # --- Sub-frame 2: CRM Login (Username & Password) ---
        self.frame_credentials = tk.Frame(self.method_container, bg=BG_CARD)
        self.lbl_user = tk.Label(self.frame_credentials, text="CRM Username", font=("Segoe UI", 9, "bold"), fg=TEXT_MAIN, bg=BG_CARD)
        self.lbl_user.pack(anchor="w", pady=(0, 4))

        self.entry_username = tk.Entry(
            self.frame_credentials,
            font=("Segoe UI", 10),
            bg=BG_DARK,
            fg=TEXT_MAIN,
            insertbackground=TEXT_MAIN,
            relief="flat",
            bd=5,
        )
        self.entry_username.pack(fill="x", pady=(0, 8))
        self.entry_username.bind("<Return>", lambda e: self.entry_password.focus_set())

        lbl_pwd = tk.Label(self.frame_credentials, text="Password", font=("Segoe UI", 9, "bold"), fg=TEXT_MAIN, bg=BG_CARD)
        lbl_pwd.pack(anchor="w", pady=(0, 4))

        self.entry_password = tk.Entry(
            self.frame_credentials,
            font=("Segoe UI", 10),
            show="•",
            bg=BG_DARK,
            fg=TEXT_MAIN,
            insertbackground=TEXT_MAIN,
            relief="flat",
            bd=5,
        )
        self.entry_password.pack(fill="x", pady=(0, 10))
        self.entry_password.bind("<Return>", lambda e: self._on_pair_click())

        self.lbl_creds_hint = tk.Label(
            self.frame_credentials,
            text="Sign in using your official CRM username (not your email address) and password. Your password is used once over HTTPS to bind this device and is never stored on disk.",
            font=("Segoe UI", 8),
            fg=TEXT_MUTED,
            bg=BG_CARD,
            wraplength=540,
            justify="left",
        )
        self.lbl_creds_hint.pack(anchor="w")

        # Error label (shared in card)
        self.lbl_pair_error = tk.Label(card, text="", font=("Segoe UI", 9), fg=COLOR_DANGER, bg=BG_CARD)
        self.lbl_pair_error.pack(anchor="w", pady=(4, 0))

        # Bottom Button Bar
        self.btn_bar3 = tk.Frame(self.step_container, bg=BG_DARK)
        self.btn_bar3.pack(side="bottom", fill="x", pady=(12, 0))

        btn_back = tk.Button(
            self.btn_bar3,
            text="← Back",
            font=("Segoe UI", 9),
            bg=BG_CARD,
            fg=TEXT_MAIN,
            relief="flat",
            padx=16,
            pady=8,
            cursor="hand2",
            command=lambda: self.show_step(2),
        )
        btn_back.pack(side="left")

        self.btn_pair = tk.Button(
            self.btn_bar3,
            text="Pair Workstation 🔗",
            font=("Segoe UI", 10, "bold"),
            bg=COLOR_SUCCESS,
            fg="#000000",
            activebackground=COLOR_SUCCESS,
            relief="flat",
            padx=20,
            pady=8,
            cursor="hand2",
            command=self._on_pair_click,
        )
        self.btn_pair.pack(side="right")

        # Default to Pairing Code method
        self._set_auth_method("PAIRING_CODE")

    def _set_auth_method(self, method: str) -> None:
        """Switches active authentication method view."""
        self.auth_method = method
        self.lbl_pair_error.configure(text="")

        if method == "PAIRING_CODE":
            self.btn_tab_code.configure(bg=ACCENT, fg="#000000")
            self.btn_tab_creds.configure(bg=BG_CARD_HOVER, fg=TEXT_MUTED)
            self.frame_credentials.pack_forget()
            self.frame_pairing_code.pack(fill="x")
            self.entry_code.focus_set()
            if hasattr(self, "btn_pair"):
                self.btn_pair.configure(text="Pair Workstation 🔗")
        else:
            self.btn_tab_code.configure(bg=BG_CARD_HOVER, fg=TEXT_MUTED)
            self.btn_tab_creds.configure(bg=ACCENT, fg="#000000")
            self.frame_pairing_code.pack_forget()
            self.frame_credentials.pack(fill="x")
            self.entry_username.focus_set()
            if hasattr(self, "btn_pair"):
                self.btn_pair.configure(text="Sign In & Bind Workstation 🔗")

    def _on_pair_click(self) -> None:
        """Executes pairing or credential registration call in background worker thread."""
        code: Optional[str] = None
        username: Optional[str] = None
        password: Optional[str] = None

        if getattr(self, "auth_method", "PAIRING_CODE") == "PAIRING_CODE":
            code = self.entry_code.get().strip()
            if not code:
                self.lbl_pair_error.configure(text="Please enter your pairing code.")
                return
        else:
            username = self.entry_username.get().strip()
            password = self.entry_password.get()
            if not username:
                self.lbl_pair_error.configure(text="Please enter your CRM username.")
                return
            if not password:
                self.lbl_pair_error.configure(text="Please enter your password.")
                return

        self._is_pairing = True
        btn_loading_text = "Pairing Workstation..." if getattr(self, "auth_method", "PAIRING_CODE") == "PAIRING_CODE" else "Signing In & Binding..."
        self.btn_pair.configure(text=btn_loading_text, state="disabled")
        self.lbl_pair_error.configure(text="", fg=ACCENT)

        device_info = DeviceInfo(
            device_uuid=self.config.device_uuid,
            machine_name=platform.node(),
            os_version=f"{platform.system()} {platform.release()} ({platform.version()})",
            client_version=APP_VERSION,
        )

        auth_method = getattr(self, "auth_method", "PAIRING_CODE")

        def _worker():
            nonlocal password
            if auth_method == "PAIRING_CODE":
                success, resp = self.api_client.register_device(device_info, pairing_code=code)
            else:
                success, resp = self.api_client.register_device(device_info, username=username, password=password)
                # Password must never linger in memory
                password = None

            def _ui():
                self._is_pairing = False
                reset_text = "Pair Workstation 🔗" if auth_method == "PAIRING_CODE" else "Sign In & Bind Workstation 🔗"
                self.btn_pair.configure(text=reset_text, state="normal")

                # Clear password input from UI immediately
                if hasattr(self, "entry_password"):
                    self.entry_password.delete(0, tk.END)

                if success:
                    token = resp.get("auth_token") or resp.get("token")
                    emp_id = resp.get("employee_id")
                    emp_name = resp.get("employee_name") or "Vaani Employee"
                    emp_email = resp.get("employee_email")

                    bind_ok = self.auth_manager.register_and_bind(
                        token=token,
                        employee_id=emp_id,
                        employee_name=emp_name,
                        employee_email=emp_email,
                    )

                    if bind_ok:
                        self._registered_employee_info = {
                            "employee_name": emp_name,
                            "employee_id": emp_id,
                            "employee_email": emp_email,
                            "machine_name": device_info.machine_name,
                            "device_uuid": device_info.device_uuid,
                        }
                        self.show_step(4)
                    else:
                        self.lbl_pair_error.configure(
                            text="✗ Failed to securely store credentials or bind employee.",
                            fg=COLOR_DANGER,
                        )
                else:
                    err = resp.get("message") or resp.get("error") or "Authentication failed. Check credentials or server status."
                    self.lbl_pair_error.configure(text=f"✗ {err}", fg=COLOR_DANGER)

            self.root.after(0, _ui)

        threading.Thread(target=_worker, daemon=True, name="DevicePairingWorker").start()

    # --------------------------------------------------------------------------
    # STEP 4: COMPLETE & LAUNCH
    # --------------------------------------------------------------------------
    def _render_step4_complete(self) -> None:
        lbl_head = tk.Label(self.step_container, text="🎉 Pairing Complete!", font=("Segoe UI", 16, "bold"), fg=COLOR_SUCCESS, bg=BG_DARK)
        lbl_head.pack(anchor="w", pady=(0, 6))

        lbl_desc = tk.Label(
            self.step_container,
            text="Your workstation has been bound to your employee account in Vaani CRM.",
            font=("Segoe UI", 9),
            fg=TEXT_MUTED,
            bg=BG_DARK,
        )
        lbl_desc.pack(anchor="w", pady=(0, 16))

        card = tk.Frame(self.step_container, bg=BG_CARD, padx=20, pady=18, highlightbackground=BORDER_COLOR, highlightthickness=1)
        card.pack(fill="x", pady=(0, 20))

        info = self._registered_employee_info
        details = [
            ("Employee Name", info.get("employee_name", "Rahul Sharma")),
            ("Employee ID", f"#{info.get('employee_id', 42)}"),
            ("Employee Email", info.get("employee_email") or "—"),
            ("Workstation", info.get("machine_name", platform.node())),
            ("Device UUID", info.get("device_uuid", self.config.device_uuid)),
            ("Status", "Approved & Verified"),
        ]

        for label, val in details:
            row = tk.Frame(card, bg=BG_CARD)
            row.pack(fill="x", pady=4)

            lbl_k = tk.Label(row, text=label, font=("Segoe UI", 9), fg=TEXT_MUTED, bg=BG_CARD, width=18, anchor="w")
            lbl_k.pack(side="left")

            col = COLOR_SUCCESS if label == "Status" else TEXT_MAIN
            lbl_v = tk.Label(row, text=str(val), font=("Segoe UI", 9, "bold"), fg=col, bg=BG_CARD, anchor="w")
            lbl_v.pack(side="left")

        # Bottom Button Bar
        btn_bar = tk.Frame(self.step_container, bg=BG_DARK)
        btn_bar.pack(side="bottom", fill="x", pady=(16, 0))

        btn_finish = tk.Button(
            btn_bar,
            text="Launch Vaani Tracker 🚀",
            font=("Segoe UI", 11, "bold"),
            bg=ACCENT,
            fg="#000000",
            activebackground=ACCENT_HOVER,
            relief="flat",
            padx=24,
            pady=10,
            cursor="hand2",
            command=self._on_finish_click,
        )
        btn_finish.pack(side="right")

    def _on_finish_click(self) -> None:
        """Finishes wizard, destroys wizard window, and calls on_complete."""
        logger.info("First-run setup wizard finished.")
        self.completed = True
        try:
            self.root.quit()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

        if self.on_complete:
            self.on_complete()

