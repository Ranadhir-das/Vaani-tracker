# Vaani Desktop Tracker — Version 1.0.0 Release Notes
**Release Date**: November 3, 2026  
**Target Environment**: Microsoft Windows 10 & Windows 11 (64-bit)  
**Distribution Channel**: Official Vaani Telecalling CRM Software Repository  

---

## 1. Executive Summary

Version 1.0.0 marks the initial enterprise release of the **Vaani Desktop Tracker**. Built specifically for telecalling agents and creative video editor departments, this release delivers a native desktop dashboard, guided device pairing flow, offline SQLite WAL resiliency, and strict employee privacy safeguards.

---

## 2. Release Artifacts & Verification Checksums

All binaries are compiled for 64-bit Windows environments. Verify your downloaded binaries against the official SHA-256 checksums below before deploying to endpoints:

| Artifact | Type | File Size | SHA-256 Checksum |
| :--- | :--- | :--- | :--- |
| **`VaaniTracker.exe`** | Standalone Executable | 21,990,943 bytes (~21.0 MB) | `C8F399E27B9794F9E7B4790D27C3AC53947599A06DEEBEBE5E3CFF3958E8A5FB` |
| **`VaaniTrackerSetup.exe`** | Windows Installer | 24,279,879 bytes (~23.2 MB) | `92DBC061BB15AFCD01DEF34A933E5888BE6966153EC783CD9BDA34531D282A21` |

---

## 3. Key Features & Capabilities in v1.0.0

### A. Modern Desktop Dashboard (Tkinter / ttk Dark Slate UI)
- **Top Header**: Vaani enterprise logo, dynamic live state badge (ACTIVE, IDLE, PAUSED, LOCKED, SLEEPING, STOPPED), bound employee name & ID, cloud sync health badge, and manual "Sync Now" button.
- **8 Dedicated Panels**:
  1. **Overview**: 6 KPI metric cards (Active Time, Idle Time, Break Time, Current State, Sync Queue, Workstation Status) plus employee privacy assurance banner.
  2. **Activity Timeline**: Chronological table of 60-second activity buckets with timestamps, duration, active vs. idle ratio, primary process, and sync status.
  3. **Application Usage**: Process breakdown showing active minutes and percentages with visual progress bars.
  4. **Device & Account**: Workstation hostname, OS version, UUID, employee identity, local database paths, with unpairing option.
  5. **Settings**: Configurable backend URL, idle threshold (seconds), sync interval, track apps toggle, and Windows startup toggle.
  6. **Privacy & Rights**: Side-by-side comparison matrix of what is recorded vs. what is strictly never collected.
  7. **Diagnostics & Logs**: Real-time log viewer, database size indicator, bucket counts, and single-click sanitized diagnostic copy.
  8. **About**: Version information, technology architecture, and website links.

### B. Guided First-Run Setup & Pairing Wizard
- 4-step wizard launching automatically when an unauthenticated workstation starts the application.
- Integrates with the CRM pairing API (`POST /api/v1/desktop-tracker/register/`).
- Validates server reachability before pairing.
- Supports 6-character pairing codes with background verification.

### C. Native Windows Hardware & Power Monitoring
- **Lock Detection**: Instant detection of `Win + L` and screen-saver locks via Win32 `WTSRegisterSessionNotification`.
- **Sleep & Suspend Timing**: Listens to `WM_POWERBROADCAST` to freeze tracking buckets immediately during system suspend and re-anchors monotonic time counters on resume without timing drift.
- **Hardware Inactivity Detection**: High-precision mouse/keyboard idle sensing via `GetLastInputInfo` (default: 60 seconds).
- **Single-Instance Mutex**: Robust kernel-level named mutex (`CreateMutexW`) ensuring only one instance runs per user session.

### D. Offline SQLite WAL Storage & Sync Resiliency
- Atomic storage using SQLite Write-Ahead Logging (`PRAGMA journal_mode=WAL`).
- Automated background sync every 60 seconds.
- Exponential backoff retry logic for offline periods.
- HTTP 409 idempotency handling to prevent duplicate bucket ingestion.

### E. Branded Visual Assets
- Multi-resolution Windows icon (`assets/vaani.ico`) containing 16, 24, 32, 48, 64, 128, and 256 pixel sizes.
- Full resolution wordmark logo (`assets/vaani-logo.png`) and square mark (`assets/vaani-icon.png`).
- Dynamic tray icon compositing the Vaani brand mark with state-colored status badges.

---

## 4. Manual Testing & Verification Checklist

Before rolling out to production employees, perform the following verification steps on a test workstation:

- [ ] **Clean Installation**:
  - Run `VaaniTrackerSetup.exe`. Verify install folder `%LOCALAPPDATA%\VaaniTracker` is created.
  - Verify Start Menu and Desktop shortcuts are created with official Vaani icon.
- [ ] **First-Run Wizard**:
  - Launch application on unauthenticated PC. Verify wizard opens.
  - Test server connection button.
  - Enter test pairing code and verify success screen shows bound employee name.
- [ ] **System Tray & Dashboard**:
  - Verify tray icon appears with active colored badge dot.
  - Double-click tray icon or click "📊 Open Dashboard". Verify dashboard window appears.
  - Click `[X]` on dashboard window. Verify window minimizes cleanly to system tray without exiting.
- [ ] **Tracking State Transitions**:
  - Leave mouse/keyboard untouched for 65 seconds. Verify tray and dashboard state transition from `ACTIVE` to `IDLE`.
  - Move mouse. Verify immediate return to `ACTIVE`.
  - Press `Win + L` to lock workstation. Wait 30 seconds, unlock. Verify a `LOCKED` interval is recorded in the timeline.
- [ ] **Sleep / Suspend Recovery**:
  - Put workstation into sleep for 2 minutes. Wake up.
  - Verify the sleep interval was recorded as `SLEEPING` and no runaway active minutes accumulated.
- [ ] **Offline Queueing & Sync**:
  - Disconnect network / disable Wi-Fi.
  - Work for 3 minutes. Verify pending count increments.
  - Re-enable Wi-Fi and click "🔄 Sync Now". Verify all pending buckets drain to 0.
- [ ] **Clean Shutdown**:
  - Right-click tray icon and choose "❌ Exit Tracker".
  - Verify process terminates cleanly and releases single-instance mutex.

---

## 5. Known Limitations & Compatibility

1. **Operating System Support**: Windows 10 (version 1809+) and Windows 11 are officially supported. Windows 7 and Windows 8.1 are unsupported due to Python 3.14 requirements.
2. **GPU Video Rendering Note (Video Editors)**: Extended passive viewing of video renders without keyboard/mouse interaction for >60 seconds is treated as idle by hardware sensors. Video editors should designate long render jobs via the CRM shift notes or break tools where applicable.
3. **Multi-User Fast User Switching**: If multiple Windows users log into the same physical computer simultaneously, each user session runs its own isolated instance using session-local mutexes (`Local\VaaniTracker_*`).

---

## 6. Privacy & Compliance Statement

Vaani Desktop Tracker complies with employee data minimization standards:
- **No Screenshots**: Screen buffers are never read or captured.
- **No Keystroke Logging**: Keyboard input content is completely inaccessible to the tracker.
- **No Web History**: Website URLs, queries, and browser tab titles are never recorded.
- **Executable Name Only**: Only foreground process file basenames (e.g. `code.exe`) are captured to verify business application usage.

