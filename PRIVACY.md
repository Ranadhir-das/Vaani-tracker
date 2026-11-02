# Vaani Desktop Activity Tracker — Privacy & Transparency Notice

**Effective Date:** November 2026  
**Audience:** Pilot Users (IT Department, Video Editor Department) & All Employees  
**Version:** 1.0.0 (MVP)

---

## 1. Guiding Privacy Principles

The Vaani Desktop Activity Tracker is designed with an uncompromising **privacy-by-design** approach. Its purpose is to verify active workspace attendance and detect inactivity during designated work shifts—**never** to monitor private activities, read text, or record employee interactions.

---

## 2. STRICT PROHIBITIONS — What We NEVER Collect

Under no circumstances will Vaani Desktop Tracker ever record or transmit the following data:

| Prohibited Category | Our Technical Guarantee |
| :--- | :--- |
| **Keystrokes & Typing** | ❌ **NEVER recorded.** We only query `GetLastInputInfo` (tick count since last physical input). Individual keys, typed words, and passwords are never read or logged. |
| **Screenshots & Video** | ❌ **NEVER captured.** No desktop screenshots, window captures, or screen recordings are taken. |
| **Webcam & Microphone** | ❌ **NEVER accessed.** The software contains zero camera or microphone APIs. |
| **Clipboard Contents** | ❌ **NEVER read.** Clipboard text, files, and copied data remain strictly private on your computer. |
| **Document & File Contents** | ❌ **NEVER inspected.** Source code, emails, spreadsheets, and document text are never opened or scanned. |
| **Browser URLs & Web History** | ❌ **NEVER recorded.** We do not capture URLs, visited domains, search queries, or tab titles. |
| **Private Messages** | ❌ **NEVER read.** Chat messages, Slack conversations, and WhatsApp web messages are never read. |

---

## 3. What IS Collected During Active Shifts

Tracking is **only enabled** when you have an active attendance shift registered in the Vaani CRM. When your shift is not started, is on break, or has ended, no work tracking occurs.

During an active shift, the application collects:

1. **System Inactivity State**:
   - **ACTIVE**: Input detected within the last 60 seconds.
   - **IDLE**: No mouse or keyboard input for 60 consecutive seconds.
   - **LOCKED**: Windows workstation is locked (`WTS_SESSION_LOCK`).
   - **SLEEPING**: Computer entered sleep or hibernation mode (`WM_POWERBROADCAST`).
   - **PAUSED**: Shift is currently on break.

2. **Transparent Application Tracking (Executable Identity Only)**:
   - When enabled by company policy, the application records only the **binary filename** of the active foreground application (for example: `code.exe`, `Adobe Premiere Pro.exe`, `chrome.exe`) and the active seconds spent in it.
   - Window titles, document names, and web addresses are strictly stripped and discarded.

3. **Device Metadata**:
   - Machine hostname, Windows OS version, and client version for secure device pairing.

---

## 4. Employee Transparency & Self-Audit

Employees have full visibility into the exact data captured:

- **Tray Status Indicator**: Shows current tracking state (Active, Idle, On Break, Stopped) at all times in the Windows system tray.
- **Activity Summary Dashboard**: Click **"View Activity Summary"** in the tray menu anytime to inspect today's recorded active time, break duration, idle periods, and application breakdown.
- **Local Storage**: All queued data is stored locally in `%LOCALAPPDATA%\VaaniTracker\events.db` where employees can inspect it.

---

## 5. Security & Device Binding

- All data is transmitted over encrypted **HTTPS (TLS 1.3)**.
- Each device is securely bound to an authenticated employee; unauthorized device reassignment is rejected.
- Revocation of access immediately halts tracking and unpairs the workstation.

---

For any questions or privacy concerns, contact the Vaani Security and Compliance Officer.

