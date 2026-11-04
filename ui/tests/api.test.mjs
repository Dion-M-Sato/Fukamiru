import test from "node:test";
import assert from "node:assert/strict";

import {
  SERVICE_START_COMMAND,
  SERVICE_UNAVAILABLE_MESSAGE,
  api,
} from "../src/api.js";

test("network failures report a stopped ふかみる service and recovery command", async () => {
  const failedFetch = async () => {
    throw new TypeError("Failed to fetch");
  };

  await assert.rejects(
    api("/api/health", {}, failedFetch),
    (error) => {
      assert.equal(error.code, "fukamiru_service_unavailable");
      assert.equal(error.message, SERVICE_UNAVAILABLE_MESSAGE);
      assert.match(error.message, /ふかみるサービス/);
      assert.ok(error.message.includes(SERVICE_START_COMMAND));
      assert.doesNotMatch(error.message, /Failed to fetch/);
      return true;
    },
  );
});

test("HTTP errors keep the API detail instead of reporting the service offline", async () => {
  const rejectedRequest = async () => ({
    ok: false,
    status: 400,
    statusText: "Bad Request",
    json: async () => ({ detail: "Revision requires feedback or corrected_text" }),
  });

  await assert.rejects(
    api("/api/reviews/example/resolve", {}, rejectedRequest),
    (error) => {
      assert.equal(error.status, 400);
      assert.equal(error.message, "Revision requires feedback or corrected_text");
      return true;
    },
  );
});
