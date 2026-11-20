/// <reference lib="webworker" />
import { createEngine, type Req, type RenderOut } from "./raster-engine.ts";

const engine = createEngine();
const scope = self as unknown as DedicatedWorkerGlobalScope;

scope.onmessage = async (e: MessageEvent<{ id: number; req: Req }>) => {
  const { id, req } = e.data;
  try {
    const out = await engine.handle(req, (text) => scope.postMessage({ id, progress: text }));
    const transfer = req.type === "render" ? (out as RenderOut).images.map((i) => i.buffer as ArrayBuffer) : [];
    scope.postMessage({ id, ok: true, out }, transfer);
  } catch (err) {
    scope.postMessage({ id, ok: false, error: (err as Error).message });
  }
};
