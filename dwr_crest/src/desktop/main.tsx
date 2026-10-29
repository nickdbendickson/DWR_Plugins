/**
 * Desktop entry: the same <Desk /> the web app renders at "/", with no server, auth or fonts from the network.
 * `npm run build:desktop` bundles this into crest-desktop/crest/web/, which the pywebview window serves locally.
 */
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { Desk } from "@/components/crest/desk";
import "../styles.css";

const root = document.getElementById("root");
if (!root) throw new Error("Crest desktop: #root is missing from index.html");
createRoot(root).render(
  <StrictMode>
    <Desk />
  </StrictMode>,
);
