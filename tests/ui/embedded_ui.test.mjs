/**
 * Behavioural tests for the embedded interface.
 *
 * The Python suite can only assert that a literal appears in the PAGE string,
 * so a change that keeps the literal and breaks the behaviour passes. These
 * drive the real document instead: they click, deliver plugin messages and
 * read the resulting DOM.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { openInterface, model } from "./harness.mjs";

function withInterface(run) {
  const ui = openInterface();
  try {
    return run(ui);
  } finally {
    ui.close();
  }
}

function openFirstCard(ui) {
  ui.document.querySelector("#results .card").dispatchEvent(
    new ui.window.MouseEvent("click", { bubbles: true }),
  );
}

test("hostile portal text cannot inject nodes or run script", () => {
  withInterface((ui) => {
    ui.window.__pwned = false;
    const hostile = '<img src=x onerror="window.__pwned=true">';
    ui.send({
      action: "results",
      results: [
        model({
          name: hostile,
          author: `<script>window.__pwned=true</script>`,
          license: hostile,
          platform: hostile,
        }),
      ],
      sources: [{ key: "printables", display: hostile, error: hostile }],
      more: false,
    });

    assert.equal(ui.document.querySelectorAll("#results img[onerror]").length, 0);
    assert.equal(ui.document.querySelectorAll("#results script").length, 0);
    assert.equal(ui.window.__pwned, false);
    // The text still has to reach the user, just as text.
    assert.equal(ui.document.querySelector("#results .card h3").textContent, hostile);
  });
});

test("a javascript: URL never reaches an image or a link", () => {
  withInterface((ui) => {
    ui.send({
      action: "results",
      results: [
        model({
          thumbnail_url: "javascript:window.__pwned=true",
          url: "javascript:window.__pwned=true",
        }),
      ],
      sources: [],
      more: false,
    });
    const image = ui.document.querySelector("#results .result-image");
    assert.equal(image.getAttribute("data-src"), "");

    openFirstCard(ui);
    assert.equal(ui.document.querySelectorAll("#det-url a").length, 0);
  });
});

test("a background details message cannot hand back an in-flight import", () => {
  withInterface((ui) => {
    const row = model();
    ui.send({ action: "results", results: [row], sources: [], more: false });
    openFirstCard(ui);

    ui.drain();
    ui.$("det-import-btn").click();
    assert.deepEqual(
      ui.drain().map((m) => m.action),
      ["resolve_import"],
    );
    assert.equal(ui.$("det-import-btn").disabled, true);

    // The Thingiverse prefetch posts one of these per result, a second later.
    ui.send({
      action: "model_details",
      model: { ...row, license: "CC BY-SA" },
      background: true,
    });

    assert.equal(ui.$("det-import-btn").disabled, true);
    assert.equal(ui.$("det-import-btn").textContent, "Resolving...");
    ui.$("det-import-btn").click();
    assert.deepEqual(ui.drain(), [], "a second resolve_import must not be possible");
  });
});

test("a details message repaints one card, not the whole grid", () => {
  withInterface((ui) => {
    const rows = [model({ url: "https://x/1" }), model({ url: "https://x/2" })];
    ui.send({ action: "results", results: rows, sources: [], more: false });
    const first = ui.document.querySelector('#results .card[data-idx="0"]');
    const second = ui.document.querySelector('#results .card[data-idx="1"]');

    ui.send({
      action: "model_details",
      model: { ...rows[0], license: "CC0" },
      background: true,
    });

    assert.equal(
      ui.document.querySelector('#results .card[data-idx="0"] .license-badge')
        .textContent,
      "CC0",
    );
    // Same nodes: rebuilding the page would replace them and reset the
    // image observer once per prefetched result.
    assert.equal(ui.document.querySelector('#results .card[data-idx="0"]'), first);
    assert.equal(ui.document.querySelector('#results .card[data-idx="1"]'), second);
  });
});

test("an abandoned sign-in does not import later when an account connects", () => {
  withInterface((ui) => {
    ui.send({ action: "auth_status", states: {} });
    ui.send({
      action: "results",
      results: [
        model({
          platform: "Thingiverse",
          _platform_key: "thingiverse",
          requires_auth: true,
          authenticated: false,
        }),
      ],
      sources: [],
      more: false,
    });
    openFirstCard(ui);

    ui.drain();
    ui.$("det-import-btn").click(); // opens the account modal instead
    assert.equal(ui.$("auth-modal").classList.contains("active"), true);

    ui.$("auth-modal").querySelector(".secondary").click();
    ui.window.closeAuth();
    ui.drain();

    // Minutes later the user connects some account from the Accounts panel.
    ui.send({
      action: "auth_changed",
      states: { thingiverse: { authenticated: true, label: "tester" } },
    });

    assert.deepEqual(
      ui.drain().filter((m) => m.action === "resolve_import"),
      [],
      "a model the user walked away from must not import itself",
    );
  });
});

test("connecting the account the user was asked for does resume the import", () => {
  withInterface((ui) => {
    ui.send({ action: "auth_status", states: {} });
    ui.send({
      action: "results",
      results: [
        model({
          platform: "Thingiverse",
          _platform_key: "thingiverse",
          requires_auth: true,
          authenticated: false,
        }),
      ],
      sources: [],
      more: false,
    });
    openFirstCard(ui);
    ui.$("det-import-btn").click();
    ui.drain();

    ui.send({
      action: "auth_changed",
      states: { thingiverse: { authenticated: true, label: "tester" } },
    });

    assert.deepEqual(
      ui.drain()
        .filter((m) => m.action === "resolve_import")
        .map((m) => m.model.platform),
      ["Thingiverse"],
    );
  });
});

test("the file picker pre-ticks only what the slicer can open", () => {
  withInterface((ui) => {
    ui.send({
      action: "file_choices",
      files: [
        { index: 0, name: "Bracket.STL", selected: true },
        { index: 1, name: "Bracket.pdf", selected: false },
        { index: 2, name: "Assembly.SLDASM", selected: false },
      ],
    });

    const boxes = [...ui.document.querySelectorAll("#file-list input[type=checkbox]")];
    assert.deepEqual(
      boxes.map((box) => box.checked),
      [true, false, false],
    );

    ui.drain();
    ui.$("file-import").click();
    assert.deepEqual(JSON.parse(JSON.stringify(ui.drain())), [
      { action: "import_selected", indices: [0] },
    ]);
  });
});

test("a portal error is shown without being trusted as markup", () => {
  withInterface((ui) => {
    ui.send({
      action: "results",
      results: [],
      sources: [
        {
          key: "cults3d",
          display: "Cults3D",
          error: '<img src=x onerror="window.__pwned=true">',
          loaded: 0,
          visible: 0,
        },
      ],
      more: false,
    });
    ui.window.__pwned = false;
    assert.equal(ui.document.querySelectorAll("#source-results img").length, 0);
    assert.equal(ui.window.__pwned, false);
    assert.match(ui.document.querySelector("#source-results small").textContent, /onerror/);
  });
});
