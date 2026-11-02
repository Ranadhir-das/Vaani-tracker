"""Employee-accessible activity summary dashboard viewer."""

from datetime import datetime, timezone
import html
from pathlib import Path
from typing import Dict, Any
import webbrowser

from ..config import TrackerConfig
from ..core.state_machine import TrackingStateMachine
from ..storage.queue import ActivityQueue


def format_duration(seconds: int) -> str:
    """Formats seconds into readable string (e.g. '2h 15m 30s')."""
    if seconds <= 0:
        return "0s"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    parts = []
    if h > 0:
        parts.append(f"{h}h")
    if m > 0:
        parts.append(f"{m}m")
    if s > 0 or not parts:
        parts.append(f"{s}s")
    return " ".join(parts)


def generate_summary_html(
    config: TrackerConfig,
    state_machine: TrackingStateMachine,
    queue: ActivityQueue,
) -> str:
    """Generates the HTML content for the employee transparency activity summary."""
    now_utc = datetime.now(timezone.utc)
    today_start_iso = now_utc.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    stats = queue.get_summary_stats(today_start_iso)

    shift = state_machine.shift_info
    state = state_machine.current_state

    emp_name = config.registered_employee_name or "Employee"
    emp_id = config.registered_employee_id or "—"
    device_uuid = config.device_uuid

    total_active_secs = stats["total_active_seconds"]
    total_idle_secs = stats["total_idle_seconds"]
    pending_count = queue.get_pending_count()

    app_rows = ""
    sorted_apps = sorted(stats["app_breakdown"].items(), key=lambda x: x[1], reverse=True)
    if sorted_apps:
        for proc, dur in sorted_apps:
            clean_proc = html.escape(proc)
            dur_str = format_duration(dur)
            pct = int((dur / max(1, total_active_secs)) * 100) if total_active_secs > 0 else 0
            pct = min(100, pct)
            app_rows += f"""
            <tr>
              <td style="padding: 10px 14px; font-family: monospace; font-weight: 600;">{clean_proc}</td>
              <td style="padding: 10px 14px; text-align: right;">{dur_str}</td>
              <td style="padding: 10px 14px; width: 140px;">
                <div style="background: rgba(255,255,255,0.08); border-radius: 4px; overflow: hidden; height: 8px;">
                  <div style="background: #38bdf8; height: 100%; width: {pct}%;"></div>
                </div>
              </td>
            </tr>
            """
    else:
        app_rows = """
        <tr>
          <td colspan="3" style="padding: 16px; text-align: center; color: #94a3b8;">
            No application metrics recorded yet for today's shift.
          </td>
        </tr>
        """

    state_badge_color = {
        "ACTIVE": "#10b981",
        "IDLE": "#f59e0b",
        "PAUSED": "#ea580c",
        "LOCKED": "#3b82f6",
        "SLEEPING": "#8b5cf6",
        "STOPPED": "#94a3b8",
        "DISCONNECTED": "#64748b",
    }.get(state.value, "#94a3b8")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Vaani Desktop Tracker · Activity Summary</title>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: #0f172a;
      color: #f8fafc;
      margin: 0;
      padding: 24px;
      line-height: 1.5;
    }}
    .container {{
      max-width: 800px;
      margin: 0 auto;
    }}
    .header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-bottom: 1px solid rgba(255, 255, 255, 0.1);
      padding-bottom: 16px;
      margin-bottom: 24px;
    }}
    .title-group h1 {{
      margin: 0 0 4px 0;
      font-size: 24px;
      font-weight: 700;
    }}
    .title-group p {{
      margin: 0;
      color: #94a3b8;
      font-size: 14px;
    }}
    .badge {{
      display: inline-block;
      padding: 6px 14px;
      border-radius: 9999px;
      font-size: 13px;
      font-weight: 600;
      background: rgba(255, 255, 255, 0.1);
      color: #fff;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 16px;
      margin-bottom: 24px;
    }}
    .card {{
      background: #1e293b;
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 12px;
      padding: 16px;
    }}
    .card-label {{
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: #94a3b8;
      margin-bottom: 6px;
    }}
    .card-value {{
      font-size: 22px;
      font-weight: 700;
      color: #f8fafc;
    }}
    .panel {{
      background: #1e293b;
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 12px;
      padding: 20px;
      margin-bottom: 24px;
    }}
    .panel h2 {{
      margin: 0 0 14px 0;
      font-size: 17px;
      color: #f1f5f9;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }}
    th {{
      text-align: left;
      color: #94a3b8;
      font-weight: 600;
      border-bottom: 1px solid rgba(255, 255, 255, 0.1);
      padding: 8px 14px;
    }}
    td {{
      border-bottom: 1px solid rgba(255, 255, 255, 0.04);
    }}
    .privacy-box {{
      background: rgba(16, 185, 129, 0.08);
      border: 1px solid rgba(16, 185, 129, 0.25);
      border-radius: 10px;
      padding: 16px;
      font-size: 13px;
      color: #a7f3d0;
      margin-top: 24px;
    }}
    .privacy-box strong {{
      color: #34d399;
      display: block;
      margin-bottom: 4px;
      font-size: 14px;
    }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <div class="title-group">
        <h1>Vaani Desktop Tracker</h1>
        <p>Transparency &amp; Activity Overview for {html.escape(emp_name)} (ID: {html.escape(str(emp_id))})</p>
      </div>
      <div>
        <span class="badge" style="border: 1px solid {state_badge_color}; color: {state_badge_color};">
          ● {state.value}
        </span>
      </div>
    </div>

    <div class="grid">
      <div class="card">
        <div class="card-label">Net Work Time Today</div>
        <div class="card-value" style="color: #38bdf8;">{format_duration(total_active_secs)}</div>
      </div>
      <div class="card">
        <div class="card-label">Idle Inactivity Today</div>
        <div class="card-value" style="color: #fbbf24;">{format_duration(total_idle_secs)}</div>
      </div>
      <div class="card">
        <div class="card-label">Shift Status</div>
        <div class="card-value" style="font-size: 18px;">{shift.status.value}</div>
      </div>
      <div class="card">
        <div class="card-label">Queue Sync Status</div>
        <div class="card-value" style="font-size: 18px;">{pending_count} pending</div>
      </div>
    </div>

    <div class="panel">
      <h2>Foreground Application Breakdown (Duration Only)</h2>
      <table>
        <thead>
          <tr>
            <th>Application Process</th>
            <th style="text-align: right;">Time Spent</th>
            <th>Share</th>
          </tr>
        </thead>
        <tbody>
          {app_rows}
        </tbody>
      </table>
    </div>

    <div class="panel">
      <h2>Device Registration Metadata</h2>
      <p style="margin: 0; font-size: 13px; color: #94a3b8;">
        Device UUID: <code style="color: #cbd5e1;">{html.escape(device_uuid)}</code><br>
        Backend URL: <code style="color: #cbd5e1;">{html.escape(config.backend_url)}</code><br>
        Idle Threshold: <strong>{config.idle_threshold_seconds}s</strong> · Sync Interval: <strong>{config.sync_interval_seconds}s</strong>
      </p>
    </div>

    <div class="privacy-box">
      <strong>🔒 Strict Privacy Guarantee</strong>
      Vaani Tracker records only whether the system is active or idle and transparent application executable basenames.
      It <strong>NEVER</strong> records keystrokes, typed text, passwords, document contents, browser URLs, screen contents, or webcam/microphone inputs.
    </div>
  </div>
</body>
</html>"""


def show_activity_summary(
    config: TrackerConfig,
    state_machine: TrackingStateMachine,
    queue: ActivityQueue,
) -> Path:
    """Writes the summary HTML file to disk and opens it in the system web browser."""
    content = generate_summary_html(config, state_machine, queue)
    summary_path = config.data_dir / "summary.html"
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(content)

    webbrowser.open(summary_path.as_uri())
    return summary_path

