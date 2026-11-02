import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { ENGINE_VERSION } from "@/lib/crest/engine";
import { num } from "@/lib/crest/format";
import {
  hydrographSvg,
  PAPER_DISCLAIMER,
  PAPER_NOTICE,
  paperFlags,
  paperRows,
  receptorCsv,
  stampClass,
} from "@/lib/crest/paper";
import { saveText } from "@/lib/crest/save";
import type { Analysis, DamRecord } from "@/lib/crest/types";

type Props = {
  dam: DamRecord;
  result: Analysis;
  /** Source of record, as the Report step names it ("Desk estimate", "LISFLOOD-FP run", ...). */
  source: string;
  /** Warnings of the imported run of record, if any. */
  runWarnings?: string[];
};

/**
 * Style 3: the Report step as one paper sheet. The on-screen sheet and the printed one are the same component with
 * the same props (a second copy is portalled to <body> for printing), so the PDF from the browser's print is what
 * the screen shows. The toolbar is screen-only.
 */
export function PaperReport(props: Props) {
  const [status, setStatus] = useState<string | null>(null);
  const [host, setHost] = useState<HTMLElement | null>(null);
  useEffect(() => {
    const el = document.createElement("div");
    el.className = "crest-print-root";
    document.body.appendChild(el);
    setHost(el);
    return () => {
      el.remove();
    };
  }, []);
  const csv = async () => {
    const root = (props.dam.nid || props.dam.id || "crest")
      .toLowerCase()
      .replace(/[^a-z0-9]/g, "_");
    const name = `${root}_structure_impacts.csv`;
    const r = await saveText(name, receptorCsv(props.result), "text/csv");
    setStatus(
      r.status === "saved"
        ? `${r.path ? `Saved ${r.path}` : `Downloaded ${name}`}.`
        : r.status === "cancelled"
          ? null
          : `Could not save ${name}: ${r.message}`,
    );
  };
  const print = () => {
    try {
      window.print();
    } catch (e) {
      setStatus(
        `Printing is not available here (${(e as Error)?.message ?? String(e)}). Use Save as PDF… above instead.`,
      );
    }
  };
  return (
    <div className="paper-wrap" data-testid="paper-report">
      <div className="paper-toolbar">
        <button type="button" onClick={print}>
          🖨 Print
        </button>
        <button type="button" onClick={() => void csv()}>
          ⬇ CSV (structure impacts)
        </button>
        <button type="button" onClick={print} title="Opens the print dialog: choose Save as PDF">
          📄 Download PDF report
        </button>
        <span className="paper-toolbar-note">This page prints exactly as shown.</span>
      </div>
      {status ? (
        <p role="status" className="paper-status">
          {status}
        </p>
      ) : null}
      <PaperSheet {...props} />
      {host ? createPortal(<PaperSheet {...props} />, host) : null}
    </div>
  );
}

function PaperSheet({ dam, result, source, runWarnings = [] }: Props) {
  const date = new Date().toLocaleDateString("en-US", {
    year: "numeric",
    month: "long",
    day: "numeric",
  });
  const level = result.screen.label;
  const wet = result.flood.receptors.filter((r) => r.flooded).length;
  const { rows, shown, total } = paperRows(result);
  const flags = paperFlags(result, runWarnings);
  const deepest = [...result.flood.receptors]
    .filter((r) => r.flooded)
    .sort((a, b) => b.depthFt - a.depthFt)[0];
  return (
    <article className="paper">
      <header className="p-title">
        <div>
          <div className="kicker">NORTH DAKOTA DAM SAFETY SCREENING</div>
          <h1>
            {dam.name}
            {dam.nid ? ` (${dam.nid})` : ""}
            <br />
            Dam-Breach Consequence Estimate
          </h1>
        </div>
        <div className={`stamp stamp--${stampClass(result.screen.level)}`}>
          DESK SCREEN: {level.toUpperCase()}
        </div>
      </header>
      <hr className="rule" />
      <div className="p-meta">
        <span>Prepared: {date}</span>
        <span>
          Method: {result.breach.label} · {result.flood.engineLabel}
        </span>
        <span>Source of record: {source}</span>
      </div>

      <table className="kpitab">
        <thead>
          <tr>
            <th scope="col">Metric</th>
            <th scope="col">Value</th>
            <th scope="col">Notes</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <th scope="row">Peak breach discharge</th>
            <td>{num(result.breach.peakCfs, 0)} cfs</td>
            <td>at breach, time-to-peak {num(result.breach.timeToPeakHr, 2)} hr</td>
          </tr>
          <tr>
            <th scope="row">Maximum flood depth</th>
            <td>{num(result.flood.maxDepthFt, 1)} ft</td>
            <td>
              {deepest
                ? `deepest structure: ${deepest.name}, ${num(deepest.depthFt, 2)} ft`
                : "no structure wet"}
            </td>
          </tr>
          <tr>
            <th scope="row">Inundated area</th>
            <td>{num(result.flood.acres, 0)} acres</td>
            <td>{result.flood.engineLabel}</td>
          </tr>
          <tr>
            <th scope="row">Structures affected</th>
            <td>
              {wet} of {result.flood.receptors.length} mapped
            </td>
            <td>{source}</td>
          </tr>
        </tbody>
      </table>

      <div className="notice">
        <b>NOTICE:</b> {PAPER_NOTICE}
      </div>

      {flags.length ? (
        <div className="notice notice--flags">
          <b>MODEL FLAGS CARRIED INTO THIS REPORT:</b>
          <ul>
            {flags.map((f) => (
              <li key={f}>{f}</li>
            ))}
          </ul>
        </div>
      ) : null}

      <figure className="p-chart">
        <div dangerouslySetInnerHTML={{ __html: hydrographSvg(result.breach.series) }} />
        <figcaption>Figure 1 — Breach hydrograph at the dam.</figcaption>
      </figure>

      <section className="p-section">
        <h2>Receptor impacts</h2>
        {rows.length ? (
          <div className="p-scroll">
            <table className="recepttab">
              <thead>
                <tr>
                  <th scope="col">Structure</th>
                  <th scope="col">Depth ft</th>
                  <th scope="col">Vel ft/s</th>
                  <th scope="col">Front arrival hr</th>
                  <th scope="col">Routed peak hr</th>
                  <th scope="col">DEM</th>
                  <th scope="col">Class</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r, i) => (
                  <tr key={`${r.name}-${i}`}>
                    <th scope="row">{r.name}</th>
                    <td>{r.depth}</td>
                    <td>{r.vel}</td>
                    <td>{r.front}</td>
                    <td>{r.peak}</td>
                    <td>{r.dem}</td>
                    <td>{r.rating}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p>No mapped structure is wet.</p>
        )}
        {total > shown ? (
          <p className="fine">
            Showing {shown} of {total} affected structures; full list in the CSV export.
          </p>
        ) : null}
        <p className="fine">
          Front arrival: the desk's travel time to the flood front (or the imported run's arrival
          raster). Routed peak and the DEM behind each depth are not computed per structure yet.
        </p>
      </section>

      <section className="p-section">
        <h2>What this means</h2>
        <p>
          Desk screen: <b>{level}</b>.
        </p>
        {result.screen.reasons.length ? (
          <ul>
            {result.screen.reasons.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
        ) : null}
        {result.screen.judgment.length ? (
          <>
            <p>For an engineer to decide:</p>
            <ul>
              {result.screen.judgment.map((r) => (
                <li key={r}>{r}</li>
              ))}
            </ul>
          </>
        ) : null}
      </section>

      <footer className="p-sign">
        <div className="line">Prepared by / date</div>
        <div className="line">Reviewed by / date</div>
        <div className="line">Accepted for filing / date</div>
      </footer>

      <div className="foot">
        Generated by Crest ({ENGINE_VERSION}){dam.county ? ` · ${dam.county} County` : ""} · {date}{" "}
        · {PAPER_DISCLAIMER}
      </div>
      <div className="paper-runfoot">{PAPER_DISCLAIMER}</div>
    </article>
  );
}
