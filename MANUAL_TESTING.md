# Vaani Desktop Activity Tracker — Manual Testing Guide

Follow this guide to thoroughly verify the Windows Desktop Activity Tracker MVP in your local development environment.

---

## 1. Prerequisites

- Windows 10 or Windows 11
- Python 3.10+ (Python 3.14 tested)
- Python virtual environment with dependencies installed:
  ```powershell
  cd E:\react\vaani-tracker
  pip install -r requirements.txt
  ```

---

## 2. Test Setup with Local Mock Server

To test the application without touching production CRM databases or servers, use the included `mock_server.py`.

### Step 1: Start the Mock Server
Open a PowerShell terminal and run:
```powershell
python mock_server.py 8085
```
Output:
```
✓ Mock Vaani Backend listening at http://127.0.0.1:8085
  Controls:
  - http://127.0.0.1:8085/mock/inspect
  - POST http://127.0.0.1:8085/mock/shift/start
  - POST http://127.0.0.1:8085/mock/shift/break
  - POST http://127.0.0.1:8085/mock/shift/resume
  - POST http://127.0.0.1:8085/mock/shift/end
```

---

## 3. Test Scenarios

### Scenario A: Device Pairing & Registration
1. In a second PowerShell window, register your workstation:
   ```powershell
   python run_dev.py --backend http://127.0.0.1:8085 --register VN-TEST-001
   ```
2. Verify output confirms pairing:
   ```
   ✓ Registration successful! Device bound to Tuhi Mukherjee (Pilot) (ID: 42).
   ```
3. Check status:
   ```powershell
   python run_dev.py --status
   ```

---

### Scenario B: Normal Active Tracking
1. Launch the tracker:
   ```powershell
   python run_dev.py --backend http://127.0.0.1:8085
   ```
2. Look at the Windows System Tray (lower-right corner near the clock).
3. Observe the tray icon:
   - **Green Badge**: Indicates **ACTIVE** state during shift.
   - Hover tooltip reads: `Vaani: Active (01h 00m)`.

---

### Scenario C: 60-Second Inactivity / Idle Detection
1. Leave your mouse and keyboard completely untouched for **60 seconds**.
2. At the 60-second mark, observe the tray icon:
   - Icon transitions to **Amber/Yellow**.
   - Hover tooltip updates to: `Vaani: Idle (Paused Input)`.
3. Move the mouse or tap a key:
   - Icon immediately transitions back to **Green (ACTIVE)**.

---

### Scenario D: Workstation Lock / Unlock
1. Press `Win + L` to lock your Windows screen.
2. Wait 10 seconds, then log back in.
3. Open the tray menu and click **"View Activity Summary"**.
4. Observe that the locked period was accurately categorized as locked without tracking private screens.

---

### Scenario E: CRM Shift Break & Resume
1. In a third PowerShell window, simulate an employee taking a break from the CRM:
   ```powershell
   Invoke-RestMethod -Uri "http://127.0.0.1:8085/mock/shift/break" -Method Post -Body '{"reason":"Lunch Break"}' -ContentType "application/json"
   ```
2. Within the 30-second heartbeat interval (or trigger "Sync Now" from tray):
   - Icon transitions to **Orange**.
   - Hover tooltip displays: `Vaani: On Break (Lunch Break)`.
   - Keyboard/mouse input does NOT accumulate work duration while on break.
3. Simulate resuming work:
   ```powershell
   Invoke-RestMethod -Uri "http://127.0.0.1:8085/mock/shift/resume" -Method Post -ContentType "application/json"
   ```
4. Icon returns to **Green (ACTIVE)**.

---

### Scenario F: End Shift Behavior
1. Simulate ending the shift from the CRM:
   ```powershell
   Invoke-RestMethod -Uri "http://127.0.0.1:8085/mock/shift/end" -Method Post -ContentType "application/json"
   ```
2. On next heartbeat:
   - Icon transitions to **Gray**.
   - Hover tooltip displays: `Vaani: Shift Not Started`.
   - Tracker stops generating activity buckets.

---

### Scenario G: Offline Resilience & Network Loss
1. Stop the mock server (`Ctrl + C` in its terminal).
2. Type or work for 2 minutes.
3. Notice the tracker transitions to `DISCONNECTED` indicator but continues safely saving 60-second activity buckets to local SQLite `%LOCALAPPDATA%\VaaniTracker\events.db`.
4. Restart the mock server (`python mock_server.py 8085`).
5. Within 60 seconds (or click **"Sync Now"** in tray):
   - Queued offline buckets are batched and uploaded over HTTPS.
   - Verify all queued buckets are ingested by opening `http://127.0.0.1:8085/mock/inspect` in your browser.

---

### Scenario H: Privacy Inspection & Employee Activity Summary
1. Right-click the system tray icon and select **"View Activity Summary"**.
2. Your default web browser will open showing:
   - Total Net Work Time today
   - Total Inactivity/Idle Time
   - Application Breakdown showing process names only (e.g. `code.exe`, `powershell.exe`) and durations
   - Zero window titles, URLs, or document names displayed.
3. Verify `http://127.0.0.1:8085/mock/inspect` has zero prohibited fields.

