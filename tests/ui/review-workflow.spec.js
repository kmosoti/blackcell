import { readFile } from "node:fs/promises";

import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

const scenario = JSON.parse(
  await readFile(new URL("review-workflow.json", import.meta.url), "utf8"),
);
const [workspaceSurface, runSurface] = scenario.surfaces;
const token = "Browser-scenario-token.0123456789-ABCDEFG";
const mediaType = "application/vnd.blackcell.presentation+json";

test("review workflow preserves typed actions, semantics, provenance, and accessibility", async ({ page }) => {
  const requests = [];
  await installRuntimeRoutes(page, requests);
  await page.routeWebSocket("**/api/v1/ui/events?*", () => {});

  await page.goto("/ui");
  await expect.poll(() => page.evaluate(() =>
    getComputedStyle(document.documentElement).getPropertyValue("--color-background").trim()
  )).toBe("#0b1020");
  expect(await page.evaluate(() =>
    getComputedStyle(document.documentElement).getPropertyValue("--space-standard").trim()
  )).toBe("1rem");
  await page.getByLabel("API bearer token").fill(token);
  await page.getByRole("button", { name: "Connect", exact: true }).click();

  await expect(page.getByRole("heading", { name: workspaceSurface.title })).toBeVisible();
  await expect(page.getByRole("form", { name: "Accept plan" })).toBeVisible();
  await expect(page.getByRole("table", { name: "Recent runs" })).toContainText("run-1");
  await expect(page.getByLabel("API bearer token")).toHaveValue("");
  expect(await page.evaluate(() => [
    ...Object.entries(localStorage),
    ...Object.entries(sessionStorage),
  ])).toEqual([]);

  await page.getByLabel("Planning mode").selectOption("generated");
  await page.getByRole("button", { name: "Accept plan", exact: true }).click();
  await expect.poll(() => requests.length).toBe(1);
  expect(requests[0]).toEqual({
    schema_version: "plan-request/v1",
    planning_mode: "generated",
  });

  await page.getByLabel("Inspect run").fill("run-1");
  await page.getByRole("button", { name: "Open", exact: true }).click();
  await expect(page.getByRole("heading", { name: runSurface.title })).toBeVisible();
  await expect(page.getByRole("figure", { name: "Plan dependency graph" })).toBeVisible();
  await expect(page.getByRole("table", { name: "Plan nodes" })).toContainText("inspect");
  await expect(page.getByRole("heading", { name: "Review findings" })).toBeVisible();
  await expect(page.getByText("P1 · Acceptance evidence is incomplete")).toBeVisible();
  await expect(page.getByRole("table", { name: "Verification evidence" })).toContainText(
    "Provenance freshness",
  );

  await page.getByRole("button", { name: "Open verified artifact" }).click();
  await expect(page.getByText("verification artifact")).toBeVisible();
  const source = page.getByText("Canonical replay source", { exact: true });
  await source.click();
  await page.getByRole("button", { name: "Load canonical JSON" }).click();
  await expect(page.getByText('"run_id": "run-1"')).toBeVisible();

  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Cancel run", exact: true }).click();
  await expect.poll(() => requests.length).toBe(2);
  expect(requests[1].schema_version).toBe("execution-cancel-run-request/v1");
  expect(requests[1].idempotency_key).toMatch(/^web-cancel-[0-9a-f-]{36}$/);

  const accessibility = await new AxeBuilder({ page }).analyze();
  expect(
    accessibility.violations.filter((item) => ["critical", "serious"].includes(item.impact)),
  ).toEqual([]);
  expect(await page.locator("blackcell-surface").evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
  expect(await page.content()).not.toContain(token);
});

test("authorization faults remain explicit and content free", async ({ page }) => {
  await page.route("**/api/v1/ui/surfaces/workspace", (route) => route.fulfill({
    status: 403,
    contentType: "application/json",
    body: JSON.stringify({ error: "authorization-denied" }),
  }));
  await page.goto("/ui");
  await page.getByLabel("API bearer token").fill(token);
  await page.getByRole("button", { name: "Connect", exact: true }).click();

  await expect(page.getByText("authorization denied", { exact: true })).toBeVisible();
  await expect(page.getByLabel("API bearer token")).toHaveValue("");
});

test("declared oversized surfaces fail before the body is consumed", async ({ page }) => {
  let socketTicketRequests = 0;
  await page.route("**/api/v1/ui/surfaces/workspace", (route) => route.fulfill({
    status: 200,
    headers: {
      "Content-Length": "16777217",
      "Content-Type": mediaType,
    },
    body: "{}",
  }));
  await page.route("**/api/v1/ui/socket-tickets", (route) => {
    socketTicketRequests += 1;
    return route.fulfill({ status: 500, body: "unexpected" });
  });
  await page.goto("/ui");
  await page.getByLabel("API bearer token").fill(token);
  await page.getByRole("button", { name: "Connect", exact: true }).click();

  await expect(page.getByText("response too large", { exact: true })).toBeVisible();
  await expect(page.getByLabel("API bearer token")).toHaveValue("");
  expect(socketTicketRequests).toBe(0);
});

async function installRuntimeRoutes(page, requests) {
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/api/v1/ui/surfaces/workspace") {
      await surfaceResponse(route, workspaceSurface);
    } else if (url.pathname === "/api/v1/ui/surfaces/runs/run-1") {
      await surfaceResponse(route, runSurface);
    } else if (url.pathname === "/api/v1/ui/socket-tickets") {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          schema_version: "execution-web-socket-ticket/v1",
          ticket: "abcdefghijklmnopqrstuvwxyzABCDEF",
          expires_in_seconds: 15,
          websocket_path: "/api/v1/ui/events",
        }),
      });
    } else if (url.pathname === "/api/v1/plans") {
      requests.push(request.postDataJSON());
      await route.fulfill({ contentType: "application/json", body: "{}" });
    } else if (url.pathname === "/api/v1/runs/run-1/cancel") {
      requests.push(request.postDataJSON());
      await route.fulfill({ contentType: "application/json", body: "{}" });
    } else if (url.pathname.endsWith("/artifacts/sha256%3Aeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee")) {
      await route.fulfill({ contentType: "text/plain", body: "verification artifact\n" });
    } else if (url.pathname === "/api/v1/runs/run-1/replay") {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ run_id: "run-1", status: "failed" }),
      });
    } else {
      await route.fulfill({
        status: 404,
        contentType: "application/json",
        body: JSON.stringify({ error: "not-found" }),
      });
    }
  });
}

async function surfaceResponse(route, surface) {
  await route.fulfill({
    contentType: mediaType,
    body: JSON.stringify(surface),
  });
}
