import { useEffect, useState } from "react";
import { fetchBackupStatus, runBackup, type BackupStatus } from "./api";

export default function BackupPanel() {
  const [status, setStatus] = useState<BackupStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = async () => setStatus(await fetchBackupStatus().catch(() => null));
  useEffect(() => {
    load();
  }, []);

  const onExport = async () => {
    setBusy(true);
    setError(null);
    setMsg(null);
    try {
      const r = await runBackup();
      setMsg(`Exported ${r.name} to your Drive.`);
      await load();
    } catch (e) {
      setError(String(e).replace("Error: ", ""));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card">
      <h2>Data &amp; backup</h2>
      <p className="muted small">
        A full JSON snapshot of your J2H4All state — plans, markers, decisions, chat — exported to a
        “J2H4All Backups” folder in your own Google Drive. Runs monthly; export anytime here.
      </p>
      {status?.last_export && (
        <p className="muted small">Last export: {new Date(status.last_export).toLocaleString()}</p>
      )}
      {status && !status.drive_authorized && (
        <p className="err small">
          Drive access isn’t granted yet — backups can’t run.{" "}
          <a href="/auth/calendar/connect">Reconnect Google</a> and approve the Drive permission.
        </p>
      )}
      {msg && <p className="ok small">{msg}</p>}
      {error && <p className="err small">{error}</p>}
      <button disabled={busy || !status?.drive_authorized} onClick={onExport}>
        {busy ? "Exporting…" : "Export now"}
      </button>
    </div>
  );
}
