/**
 * Loads the plugin's embedded interface into a real DOM.
 *
 * The Python tests can only assert that a literal appears somewhere in the
 * PAGE string, which says nothing about what the interface does with it. This
 * runs the actual document, so a test can click, post a message and read the
 * result out of the DOM.
 */

import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { JSDOM } from "jsdom";

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, "..", "..");

let cachedDocument = null;

function pageHtml() {
  if (cachedDocument === null) {
    const python = process.env.PYTHON || "python";
    cachedDocument = execFileSync(
      python,
      [path.join("scripts", "export_embedded_ui.py")],
      { cwd: repo, encoding: "utf8", maxBuffer: 32 * 1024 * 1024 },
    );
  }
  return cachedDocument;
}

/**
 * Build a fresh interface. Returns the window plus the messages the page has
 * sent back to the plugin, which is how most behaviour is observed.
 */
export function openInterface() {
  const posted = [];
  let handler = null;

  const dom = new JSDOM(pageHtml(), {
    runScripts: "dangerously",
    pretendToBeVisual: true,
    url: "https://plugin.local/",
    beforeParse(window) {
      window.orca = {
        postMessage(message) {
          posted.push(message);
        },
        onMessage(callback) {
          handler = callback;
        },
      };
      // jsdom has no layout, so the observer never fires on its own.
      window.IntersectionObserver = class {
        observe() {}
        unobserve() {}
        disconnect() {}
      };
    },
  });

  const { window } = dom;
  return {
    window,
    document: window.document,
    posted,
    /** Deliver a message from the plugin, the way orca.onMessage would. */
    send(message) {
      if (!handler) throw new Error("the page never registered a message handler");
      handler(message);
    },
    /** Everything the page posted since the last call. */
    drain() {
      return posted.splice(0, posted.length);
    },
    $(id) {
      return window.document.getElementById(id);
    },
    close() {
      window.close();
    },
  };
}

/** A search result with the keys every renderer dereferences. */
export function model(overrides = {}) {
  return {
    name: "Calibration cube",
    author: "Tester",
    platform: "Printables",
    _platform_key: "printables",
    url: "https://www.printables.com/model/1-cube",
    thumbnail_url: "https://media.printables.com/cube.png",
    license: "CC BY",
    license_summary: "Credit the author.",
    requires_auth: false,
    importable: true,
    authenticated: true,
    direct_import: true,
    is_free: true,
    downloads: 10,
    likes: 2,
    result_type: "model",
    ...overrides,
  };
}
