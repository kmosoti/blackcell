import { createReadStream } from "node:fs";
import { createServer } from "node:http";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const assets = new Map([
  ["/ui", ["src/blackcell/interfaces/http/assets/ui/index.html", "text/html; charset=utf-8"]],
  ["/ui/assets/app.css", ["src/blackcell/interfaces/http/assets/ui/app.css", "text/css; charset=utf-8"]],
  ["/ui/assets/app.js", ["src/blackcell/interfaces/http/assets/ui/app.js", "text/javascript; charset=utf-8"]],
  ["/ui/assets/runtime-client.js", ["src/blackcell/interfaces/http/assets/ui/runtime-client.js", "text/javascript; charset=utf-8"]],
  ["/ui/assets/surface-elements.js", ["src/blackcell/interfaces/http/assets/ui/surface-elements.js", "text/javascript; charset=utf-8"]],
  ["/ui/assets/tokens.json", ["src/blackcell/interfaces/presentation/tokens.json", "application/json"]],
]);

const server = createServer((request, response) => {
  const path = new URL(request.url || "/", "http://127.0.0.1").pathname;
  const asset = assets.get(path);
  if (asset === undefined) {
    response.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" });
    response.end("not-found");
    return;
  }
  response.writeHead(200, {
    "Cache-Control": "no-store",
    "Content-Type": asset[1],
    "X-Content-Type-Options": "nosniff",
  });
  createReadStream(resolve(root, asset[0])).pipe(response);
});

server.listen(4173, "127.0.0.1");
process.on("SIGTERM", () => server.close());
