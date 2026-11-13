import { createRootRoute, HeadContent, Outlet, Scripts } from "@tanstack/react-router";
import appCss from "../styles.css?url";

const APP_NAME = "Crest";

// The web shell loads nothing from a third-party origin. It used to link two Google Fonts (IBM Plex Sans and Source
// Serif 4) that no rule in styles.css ever used; the link was removed on 2026-11-13 with the Content-Security-Policy
// (src/lib/crest/csp.ts), so the desk's only network use is the basemap tiles and the on-request lookups.

export const Route = createRootRoute({
  head: () => ({
    meta: [
      { charSet: "utf-8" },
      { name: "viewport", content: "width=device-width, initial-scale=1" },
      { title: APP_NAME },
      {
        name: "description",
        content:
          "Crest is a standalone dam-breach desk: elevations, breach geometry, a schematic flood, and a screening view.",
      },
      { name: "theme-color", content: "#1c2420" },
    ],
    links: [
      { rel: "icon", type: "image/svg+xml", href: "/favicon.svg" },
      { rel: "apple-touch-icon", href: "/apple-touch-icon.png" },
      { rel: "stylesheet", href: appCss },
    ],
  }),
  component: () => (
    <html lang="en" suppressHydrationWarning>
      <head>
        <HeadContent />
      </head>
      <body>
        <Outlet />
        <Scripts />
      </body>
    </html>
  ),
});
