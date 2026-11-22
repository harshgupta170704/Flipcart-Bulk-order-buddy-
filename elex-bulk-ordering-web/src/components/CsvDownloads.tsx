import { useCallback, useEffect, useState } from "react";
import { downloadExportAgain, errorMessage, listExports, type ExportLogRow } from "../lib/api";
import { formatDateTime } from "../lib/format";
import { Button } from "./Button";
import { Card } from "./Card";
import { Table, type Column } from "./Table";

/** Order Units History's "CSV Downloads": every export made, to download again. */
export function CsvDownloads() {
  const [rows, setRows] = useState<ExportLogRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<number | null>(null);

  const load = useCallback(async () => {
    try {
      setRows(await listExports());
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const again = async (row: ExportLogRow) => {
    setBusy(row.id);
    try {
      await downloadExportAgain(row);
      await load();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(null);
    }
  };

  const columns: Column<ExportLogRow>[] = [
    { key: "created_at", header: "Created", render: (row) => formatDateTime(row.created_at) },
    { key: "file_name", header: "File", render: (row) => <span className="font-mono text-[12px]">{row.file_name}</span> },
    { key: "kind", header: "Sheet", render: (row) => row.kind.replace(/_/g, " ") },
    {
      key: "query",
      header: "Filters",
      render: (row) => <span className="break-all text-[12px] text-ink-2">{decodeURIComponent(row.query.replace(/\+/g, " ")) || "None"}</span>,
    },
    { key: "created_by", header: "By", render: (row) => row.created_by ?? "—" },
    {
      key: "actions",
      header: "",
      render: (row) => (
        <Button size="sm" loading={busy === row.id} onClick={() => void again(row)}>
          Download again
        </Button>
      ),
    },
  ];

  return (
    <Card
      title="CSV Downloads"
      description="Every Order Units export, newest first. The files are not kept — they hold card numbers — so Download again builds the same export afresh from today's data."
      actions={
        <Button size="sm" variant="secondary" onClick={() => void load()}>
          Refresh
        </Button>
      }
      flush
    >
      {error ? <p className="px-5 pb-3 text-[13px] text-ink">{error}</p> : null}
      <Table caption="CSV downloads" columns={columns} rows={rows ?? []} rowKey={(row) => row.id} dense />
    </Card>
  );
}

export default CsvDownloads;
