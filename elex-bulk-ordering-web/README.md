# Bulk ordering — web UI

The screens of the bulk-ordering tool: the dashboard, the ledger of every unit
bought (Order Units), accounts and their sign-ins, coupons, placing orders,
alerts, cancel and reschedule requests, addresses and GSTs, unit limits, users
and settings.

React, TypeScript, Vite and Tailwind. It holds no data of its own: everything
comes from the API, a separate repository, **elex-bulk-ordering-bakend**. The
two are checked out side by side:

```
some-folder/
  elex-bulk-ordering-bakend/   the API, and docker-compose.yml for the whole stack
  elex-bulk-ordering-web/      this repository
```

## Run it on your own machine

Node 22 or newer, and the API running on port 8000 (see its README).

```bash
npm install        # first time only
npm run dev        # http://localhost:5173
```

The browser only ever talks to this address. `/api` and `/ws` are passed on to
the API — by Vite here (`vite.config.ts`), by nginx in production
(`nginx.conf`) — so there is no cross-origin set-up and no API address to
build in.

## Checks

```bash
npm run typecheck  # TypeScript
npm test           # the unit tests (vitest)
npm run build      # type check, then the production build into dist/
npm run preview    # serve that build on http://localhost:4173
```

## In production

`Dockerfile` builds the app and serves it with nginx. It is not started on its
own: `docker compose up --build` in the API repository builds this image from
the folder beside it and runs it with the rest. `nginx.conf` is part of the
security set-up (what is logged, upload limits, the script policy); the API's
tests check it, so change it with the API repository checked out beside this
one and run its tests.

## Where things are

| Path | What it is |
|------|------------|
| `src/pages/` | One file per screen |
| `src/components/` | The pieces the screens are built from |
| `src/lib/api.ts` | Every call to the API |
| `src/lib/types.ts` | The shapes the API answers with |
| `nginx.conf`, `docker-entrypoint.d/` | The production web server |
