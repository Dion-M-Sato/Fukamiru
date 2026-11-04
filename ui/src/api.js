export const SERVICE_START_COMMAND = 'PowerShell -ExecutionPolicy Bypass -File ".\\scripts\\windows\\start.ps1"';

export const SERVICE_UNAVAILABLE_MESSAGE =
  `ふかみるサービスが停止しているか、接続できません。` +
  `ふかみるのリポジトリで ${SERVICE_START_COMMAND} を実行してください。`;

function markServiceOffline() {
  if (typeof document === "undefined") return;
  const connectionStatus = document.getElementById("connection-status");
  if (connectionStatus) connectionStatus.textContent = "SERVICE OFFLINE";
  const statusDot = document.getElementById("tab-status-dot");
  if (statusDot) statusDot.className = "status-dot error";
}

export async function api(path, options = {}, fetchImplementation = fetch) {
  let response;
  try {
    response = await fetchImplementation(path, {
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
  } catch (cause) {
    markServiceOffline();
    const error = new Error(SERVICE_UNAVAILABLE_MESSAGE, { cause });
    error.code = "fukamiru_service_unavailable";
    throw error;
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => ({ detail: response.statusText }));
    const error = new Error(payload.detail || response.statusText);
    error.status = response.status;
    throw error;
  }
  return response.json();
}
