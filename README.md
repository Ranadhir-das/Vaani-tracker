# Vaani Windows Desktop Activity Tracker (MVP)

A lightweight, privacy-first Windows system-tray application for workforce attendance verification and inactivity monitoring in the Vaani ecosystem.

---

## 1. Overview & Scope

- **Initial Pilot Users**: IT Department & Video Editor Department.
- **Future Scope**: All Vaani enterprise departments.
- **Role**: Companion desktop sensor to the Vaani Web CRM and Android Caller App.
- **Core Principle**: **The Django backend is strictly authoritative for shift status.** The desktop tracker never independently starts or ends an attendance shift; it tracks activity **only** while an authenticated shift is active.

---

## 2. Key Architecture & Capabilities

```
┌────────────────────────────────────────────────────────────────────────┐
│                       WINDOWS DESKTOP TRACKER                          │
├────────────────────────────────────────────────────────────────────────┤
│  [Windows User Session Notification: WTSRegisterSessionNotification]  │
│  [Power Management Broadcast: WM_POWERBROADCAST (Sleep/Resume)]        │
│  [Hardware Inactivity Timer: user32.dll GetLastInputInfo (60s)]        │
│  [Foreground App Identifier: GetForegroundWindow & Process Name]       │
├────────────────────────────────────────────────────────────────────────┤
│                                   │                                    │
│                                   ▼                                    │
│                 [1-Minute Local Aggregator Engine]                     │
│               (Active Seconds, Idle Seconds, App Name)                 │
│                                   │                                    │
│                                   ▼                                    │
│                 [Local SQLite Write-Ahead Log Queue]                   │
│                    (%LOCALAPPDATA%\VaaniTracker\events.db)             │
│                                   │                                    │
│                                   ▼                                    │
│               [HTTPS Batched Synchronization Worker]                   │
│              (Every 60s / Exponential Backoff / UUIDs)                 │
│                                   │                                    │
│                                   ▼                                    │
│                 [Vaani Django CRM REST API Backend]                    │
└────────────────────────────────────────────────────────────────────────┘
```

### Tracking States
- **ACTIVE**: Keyboard/mouse activity detected within 60s during an active shift.
- **IDLE**: Inactive for $\ge$ 60 seconds during an active shift.
- **LOCKED**: Windows workstation is locked (`Win + L` / `WTS_SESSION_LOCK`).
- **SLEEPING**: Computer entered sleep or suspend power state (`WM_POWERBROADCAST`).
- **PAUSED**: Work shift is paused on break in the CRM.
- **DISCONNECTED**: Backend API is temporarily unreachable; queueing events locally.
- **STOPPED**: Shift not started, shift ended, or employee logged out.

### Strict Privacy Protections
- **Zero Keystroke Logging**: Only checks physical input timestamp via `GetLastInputInfo`.
- **Zero Screenshots / Video**: Never captures screen pixels or window contents.
- **Zero Webcam / Mic Access**: Does not open camera or audio devices.
- **Zero Clipboard Reading**: Never inspects copied text or clipboard history.
- **Zero URL / Document Tracking**: Only captures foreground executable basename (`code.exe`, `premiere.exe`). Never records document paths, window titles, or web URLs.
- **Employee Transparency**: Employees can open their local **Activity Summary** anytime from the tray icon to audit all recorded data.

---

## 3. Directory Layout

```
E:\react\vaani-tracker\
├── README.md               # Architecture and setup guide
├── PRIVACY.md              # Official privacy and transparency notice
├── MANUAL_TESTING.md       # Step-by-step manual test runbook
├── config.example.json     # Configuration file template
├── requirements.txt        # Runtime dependencies (Pillow, pystray)
├── requirements-dev.txt    # Dev & packaging dependencies (PyInstaller)
├── run_dev.py              # Development launcher
├── mock_server.py          # Standalone local mock Vaani backend
├── build_exe.py            # Automated PyInstaller build script
├── vaani_tracker.iss       # Inno Setup Windows installer script
├── src/
│   └── vaani_tracker/
│       ├── __init__.py
│       ├── __main__.py     # CLI entry point
│       ├── app.py          # Application orchestrator & lifecycle
│       ├── config.py       # Configuration & %LOCALAPPDATA% paths
│       ├── constants.py    # States, thresholds, Win32 constants
│       ├── models.py       # Dataclasses & API payloads
│       ├── privacy.py      # Strict privacy filter & payload auditor
│       ├── storage/        # SQLite WAL queue with thread-safety & idempotency
│       ├── client/         # HTTPS API client & device auth manager
│       ├── windows/        # Native Windows APIs (idle, session, power, apps)
│       ├── core/           # State machine, 1-min aggregator, background syncer
│       └── ui/             # System tray icon, menu, and HTML activity summary
└── tests/                  # 40 comprehensive unit & integration tests
```

---

## 4. Setup & Installation

### Option A: Running from Source (Development)
```powershell
# Navigate to project directory
cd E:\react\vaani-tracker

# Install dependencies in virtual environment
pip install -r requirements.txt

# Run unit and integration tests
python -m unittest discover tests

# Check tracker status
python run_dev.py --status
```

### Option B: Building Single Windows Executable (`.exe`)
```powershell
# Compile to dist\VaaniTracker.exe
python build_exe.py --onefile
```

### Option C: Building Inno Setup Windows Installer
Open `vaani_tracker.iss` in [Inno Setup Compiler](https://jrsoftware.org/isdl.php) and click **Compile**, or run:
```powershell
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" vaani_tracker.iss
```
Generates `dist\VaaniTrackerSetup.exe` with optional auto-start on Windows boot.

---

## 5. Local Testing with Mock Backend

A fully functional mock server is included to test the tracker without modifying production databases:

1. **Start Mock Server**:
   ```powershell
   python mock_server.py 8085
   ```
2. **Register Workstation**:
   ```powershell
   python run_dev.py --backend http://127.0.0.1:8085 --register VN-PILOT-01
   ```
3. **Launch Desktop Tracker**:
   ```powershell
   python run_dev.py --backend http://127.0.0.1:8085
   ```
4. **Follow `MANUAL_TESTING.md`** to verify idle transitions, locks, breaks, and summary inspection.

---

## 6. Known Limitations & Production Considerations

1. **Windows Only for Native APIs**: Hardware idle (`GetLastInputInfo`), session notifications (`WTSRegisterSessionNotification`), and power broadcast messages require Windows 10/11. The codebase includes fallback providers for non-Windows developer environments.
2. **Elevated Processes**: If an employee runs an administrative application (`Run as Administrator`), Windows User Account Control (UAC) security boundaries may prevent standard-user processes from querying the foreground process name. The tracker gracefully logs `unknown` without crashing.
3. **Multiple Monitors / Workspaces**: Activity is aggregate per physical workstation session; multiple monitors are treated as a single unified desktop.
4. **Backend Integration**: This MVP connects to the standard audited `/api/v1/desktop-tracker/*` REST endpoints. Full server-side PC reporting views will be introduced in subsequent phases.

