# Vaani Desktop Tracker — Multi-Employee Enterprise Rollout Guide
**Target Departments**: IT Department & Video Editor Department  
**Client Version**: 1.0.0 Production Release  
**Operating Systems**: Windows 10 (64-bit), Windows 11 (64-bit)  
**Authoritative Backend**: Vaani Telecalling CRM  

---

## 1. Executive Summary & Rollout Objectives

The **Vaani Desktop Tracker** provides automated, privacy-respecting work duration verification and inactivity auditing for remote and office-based employees.

- **Primary Goal**: Replace manual timesheet logging with authoritative, hardware-verified shift active time.
- **Privacy Core**: Protect employee trust by collecting **only** aggregate input presence seconds and process binary names (`code.exe`, `Adobe Premiere Pro.exe`). No invasive monitoring (no screenshots, keystrokes, URLs, or webcams).
- **Target Audience for Initial Rollout**:
  1. **IT Department**: Software engineers, DevOps, and systems administrators.
  2. **Video Editor Department**: Creative media editors utilizing GPU-heavy video rendering suites (Premiere Pro, After Effects, DaVinci Resolve).

---

## 2. Department-Specific Rollout Strategies

### A. IT Department Rollout
- **Work Profile**: High keyboard/terminal interaction, multi-monitor setups, frequent virtual machines / remote desktop sessions, code editing in VS Code, PyCharm, and Windows Terminal.
- **Key Considerations**:
  - Developers frequently keep terminals or background compilations running. The tracker accurately measures active physical input rather than CPU load.
  - Developers often run local web servers (ports 8000, 8080). The tracker binds zero local network listening ports; it operates purely as an HTTPS client connecting outward to the CRM.
  - Developers may lock workstations (`Win + L`) when away. The tracker captures lock transitions instantly via native `WTSRegisterSessionNotification`.

### B. Video Editor Department Rollout
- **Work Profile**: Extended rendering sessions, timeline scrubbing with mouse/trackball, keyboard shortcuts, video playback, large media assets.
- **Key Considerations**:
  - During long render/export jobs in Adobe Premiere Pro or DaVinci Resolve, an editor might not interact with keyboard/mouse for 10–30 minutes while watching rendering progression.
  - **Best Practice for Video Editors**: Configure or remind editors to record official breaks or designate render intervals as active project sessions in the CRM where applicable.
  - The tracker accurately identifies `Adobe Premiere Pro.exe`, `AfterFX.exe`, and `Resolve.exe` as active applications.

---

## 3. Installation & Distribution Procedures

IT administrators can distribute the tracker via two deployment channels:

### Option 1: Standard Guided Installation (Recommended for Employees)
1. Download `VaaniTrackerSetup.exe` from the internal portal.
2. Double-click `VaaniTrackerSetup.exe`.
3. Follow the modern installation wizard:
   - Target directory: `%LOCALAPPDATA%\Programs\VaaniTracker` (or `C:\Program Files\VaaniTracker`).
   - Checkbox: "Create a desktop shortcut" (Recommended).
   - Checkbox: "Automatically launch Vaani Tracker when Windows starts" (Recommended).
4. Click **Install**.
5. On completion, check **Launch Vaani Desktop Tracker** and click **Finish**.

### Option 2: Silent Corporate IT Deployment (PowerShell / Active Directory GPO)
Administrators deploying across multiple workstation endpoints can run the installer silently:

```powershell
# Silent installation without user prompts
Start-Process -FilePath ".\VaaniTrackerSetup.exe" -ArgumentList "/VERYSILENT /NORESTART /TASKS=`"startupicon,desktopicon`"" -Wait
```

---

## 4. Workstation Provisioning & Pairing Workflow

Every workstation running the tracker must be securely bound to an authorized employee account in Vaani CRM.

```
┌──────────────────┐       1. Generate Pairing Code       ┌────────────────────────┐
│  CRM Supervisor  │ ───────────────────────────────────> │  Vaani Web CRM Portal  │
└──────────────────┘                                      └────────────────────────┘
                                                                       │
                                                            2. Code: "VN-9428"
                                                                       │
                                                                       ▼
┌──────────────────┐        3. Enter Code in Wizard       ┌────────────────────────┐
│ Employee Desktop │ <─────────────────────────────────── │  First-Run Setup Flow  │
└──────────────────┘                                      └────────────────────────┘
         │
         │ 4. POST /api/v1/desktop-tracker/register/
         ▼
┌────────────────────────┐
│ Vaani Backend Approval │ ──> Returns Auth Token & Binds Employee ID
└────────────────────────┘
```

### Step-by-Step Pairing Flow:
1. **Manager Action**: The supervisor navigates to **CRM > Employee Management > Devices** and clicks **Generate Workstation Pairing Code** for the employee (e.g., `VN-9428`, valid for 24 hours).
2. **Employee Action**: Launch Vaani Desktop Tracker.
3. If unauthenticated, the **Setup & Pairing Wizard** opens automatically:
   - **Step 1: Welcome & Privacy Commitment**: Employee reviews what is and is not collected.
   - **Step 2: Server Connection**: Verify or input the CRM Backend URL (`https://vaaniapp.co.in`). Click **Test Connection**.
   - **Step 3: Device Pairing**: Enter the 6-character code (`VN-9428`) and click **Pair Workstation**.
   - **Step 4: Complete**: Workstation displays green confirmation with employee name and ID.
4. Click **Launch Vaani Tracker**. The tracker immediately docks into the Windows system tray and opens the Dashboard.

---

## 5. Network, Firewall & Security Requirements

| Parameter | Specification | Notes |
| :--- | :--- | :--- |
| **Outbound Protocol** | HTTPS (Port 443 / TLS 1.2+) | Outbound requests only. Zero inbound listening ports. |
| **API Endpoints** | `/api/v1/desktop-tracker/*` | `register/`, `status/`, `sync/` |
| **Target Host** | Production CRM (`https://vaaniapp.co.in`) | Configurable in `config.json` |
| **Proxy Compatibility** | Standard Windows system HTTP/HTTPS proxy | Supported via Python `urllib` |
| **Privileges Required** | Standard User (`lowest`) | No administrative elevation needed |

---

## 6. Offline Tolerance & Data Resiliency

The tracker includes built-in offline resiliency:
- **Local Storage**: All 60-second activity buckets are committed locally to an encrypted/isolated SQLite Write-Ahead Log database at `%LOCALAPPDATA%\VaaniTracker\events.db`.
- **Automatic Queueing**: If the internet or VPN disconnects, buckets remain safely queued with `sync_status = PENDING`.
- **Automatic Ingestion Catch-Up**: As soon as connectivity returns, the background syncer drains queued buckets in batches of 50 using exponential backoff.
- **Zero Data Loss Guarantee**: Local events are marked `SYNCED` only after the server acknowledges ingestion. HTTP 409 conflict handling prevents duplicate counting.

---

## 7. IT Maintenance, Diagnostics & Unpairing

### Viewing Real-Time Logs
Logs are maintained with automatic rotation at:
`%LOCALAPPDATA%\VaaniTracker\tracker.log`

To inspect live diagnostics directly from the desktop:
1. Double-click the Vaani Tracker system tray icon or right-click and choose **📊 Open Dashboard**.
2. Navigate to the **🔍 Diagnostics & Logs** tab.
3. Click **📋 Copy Diagnostic Info** to generate a sanitized diagnostic report with all authentication tokens masked.

### Workstation Re-assignment / Employee Offboarding
When reassigning a computer to another employee:
1. Open the dashboard and navigate to **🖥️ Device & Account**.
2. Click **⚠️ Unpair / Re-register Device**.
3. Confirm unpairing. Local tokens are deleted immediately, unlinking the previous employee profile.
4. Run the wizard with the new employee's pairing code.

