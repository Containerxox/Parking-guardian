// API 서버와 통신하는 유일한 모듈.
// Base URL, Access Token 보관, 401 발생 시 Refresh 후 재시도 규칙을 여기서만 안다.
// 계약: docs/design/API.md

const API_BASE = (
  process.env.REACT_APP_API_BASE || "http://localhost:5000/api/v1"
).replace(/\/+$/, "");

// Access Token은 메모리에만 둔다 (localStorage 금지).
let accessToken = null;
let authHandlers = {};
let refreshPromise = null;

const DEFAULT_MESSAGES = {
  400: "입력값을 확인해 주세요.",
  401: "로그인이 필요합니다.",
  403: "접근 권한이 없습니다.",
  404: "요청한 정보를 찾을 수 없습니다.",
  409: "현재 상태에서는 처리할 수 없는 요청입니다.",
  503: "서비스를 일시적으로 사용할 수 없습니다.",
};

function makeError(status, code, message) {
  const err = new Error(message);
  err.status = status;
  err.code = code;
  return err;
}

// 인증 계층(AuthContext)이 등록한다.
// onUnauthorized: Refresh까지 실패해 로그인이 풀렸을 때
// onRefreshed(admin): Refresh가 성공해 Admin 정보가 갱신됐을 때
export function setAuthHandlers(handlers) {
  authHandlers = handlers || {};
}

export function clearAccessToken() {
  accessToken = null;
}

function buildUrl(path, query) {
  let url = API_BASE + path;
  if (query) {
    const params = new URLSearchParams();
    Object.keys(query).forEach((key) => {
      const value = query[key];
      if (value !== undefined && value !== null && value !== "") {
        params.append(key, String(value));
      }
    });
    const qs = params.toString();
    if (qs) url += `?${qs}`;
  }
  return url;
}

async function send(path, { method = "GET", query, body, withToken = true } = {}) {
  const headers = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (withToken && accessToken) headers.Authorization = `Bearer ${accessToken}`;
  try {
    return await fetch(buildUrl(path, query), {
      method,
      headers,
      credentials: "include",
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    throw makeError(0, "network_error", "서버와 연결할 수 없습니다.");
  }
}

async function parse(res) {
  let data = null;
  const text = await res.text();
  if (text) {
    try {
      data = JSON.parse(text);
    } catch (e) {
      data = null;
    }
  }
  if (!res.ok) {
    const error = data && data.error ? data.error : {};
    throw makeError(
      res.status,
      error.code || "unknown_error",
      error.message || DEFAULT_MESSAGES[res.status] || `요청에 실패했습니다. (${res.status})`
    );
  }
  return data;
}

// POST /auth/refresh. 동시에 여러 요청이 401을 받아도 Refresh는 한 번만 보낸다.
export function refreshSession() {
  if (!refreshPromise) {
    refreshPromise = (async () => {
      const res = await send("/auth/refresh", { method: "POST", withToken: false });
      const data = await parse(res);
      accessToken = data.access_token;
      if (authHandlers.onRefreshed) authHandlers.onRefreshed(data.admin);
      return data;
    })().finally(() => {
      refreshPromise = null;
    });
  }
  return refreshPromise;
}

function sessionLost() {
  accessToken = null;
  if (authHandlers.onUnauthorized) authHandlers.onUnauthorized();
  return makeError(401, "unauthorized", "로그인이 만료되었습니다. 다시 로그인해 주세요.");
}

async function request(path, options = {}) {
  const tokenUsed = accessToken;
  let res = await send(path, options);

  if (res.status === 401 && !options.skipRefresh) {
    // 다른 요청이 이미 Token을 갱신했다면 Refresh 없이 바로 재시도한다.
    if (accessToken === tokenUsed) {
      try {
        await refreshSession();
      } catch (err) {
        if (err.status === 0) throw err; // 네트워크 오류는 로그아웃 사유가 아니다.
        throw sessionLost();
      }
    }
    res = await send(path, options);
    if (res.status === 401) throw sessionLost();
  }
  return parse(res);
}

const get = (path, query) => request(path, { query });
const post = (path, body) => request(path, { method: "POST", body });
const patch = (path, body) => request(path, { method: "PATCH", body });
const put = (path, body) => request(path, { method: "PUT", body });

// ---------- 인증 ----------
export const authApi = {
  async login(email, password) {
    const data = await request("/auth/login", {
      method: "POST",
      body: { email, password },
      skipRefresh: true,
      withToken: false,
    });
    accessToken = data.access_token;
    return data; // {access_token, expires_in, admin}
  },
  refresh: refreshSession,
  async logout() {
    try {
      return await request("/auth/logout", { method: "POST", skipRefresh: true });
    } finally {
      accessToken = null;
    }
  },
  me: () => get("/auth/me"), // {admin}
};

// ---------- 대시보드 ----------
export const dashboardApi = {
  summary: () => get("/dashboard/summary"),
};

// ---------- 회사 ----------
export const companiesApi = {
  list: () => get("/companies"),
  get: (id) => get(`/companies/${id}`),
  create: (name) => post("/companies", { name }),
  update: (id, fields) => patch(`/companies/${id}`, fields), // {name?, status?}
};

// ---------- 관리자 ----------
export const adminsApi = {
  list: (query) => get("/admins", query), // {company_id?}
  create: (fields) => post("/admins", fields), // {email, password, name, company_id}
  update: (id, fields) => patch(`/admins/${id}`, fields), // {name?, status?, password?}
  setParkingLots: (id, parkingLotIds) =>
    put(`/admins/${id}/parking-lots`, { parking_lot_ids: parkingLotIds }),
};

// ---------- 주차장 ----------
export const parkingLotsApi = {
  list: (query) => get("/parking-lots", query), // {company_id?}
  get: (id) => get(`/parking-lots/${id}`), // ParkingLot + zones, machines, admins
  create: (fields) => post("/parking-lots", fields), // {name, address, company_id?}
  update: (id, fields) => patch(`/parking-lots/${id}`, fields), // {name?, address?, status?}
};

// ---------- 구역 ----------
export const zonesApi = {
  create: (parkingLotId, fields) => post(`/parking-lots/${parkingLotId}/zones`, fields), // {zone_name, location_description?, machine_id?}
  update: (id, fields) => patch(`/zones/${id}`, fields),
};

// ---------- 장비 ----------
export const machinesApi = {
  list: (query) => get("/machines", query), // {parking_lot_id?, company_id?}
  create: (fields) => post("/machines", fields), // {parking_lot_id, serial_number, name} -> {machine, api_key}
  update: (id, fields) => patch(`/machines/${id}`, fields), // {name?, status?}
  rotateKey: (id) => post(`/machines/${id}/rotate-key`), // -> {machine, api_key}
};

// ---------- 위반 ----------
export const violationsApi = {
  list: (query) => get("/violations", query), // {status?, parking_lot_id?, company_id?, page?, size?}
  get: (id) => get(`/violations/${id}`), // Violation + history
  acknowledge: (id) => post(`/violations/${id}/acknowledge`),
  resolve: (id, resolutionNote) =>
    post(`/violations/${id}/resolve`, resolutionNote ? { resolution_note: resolutionNote } : {}),
  reopen: (id, message) => post(`/violations/${id}/reopen`, { message }),
  imageUrl: (id) => get(`/violations/${id}/image-url`), // {url, expires_in}
};

// ---------- 알림 ----------
export const notificationsApi = {
  list: (query) => get("/notifications", query), // {unread?, page?, size?}
  read: (id) => post(`/notifications/${id}/read`),
  readAll: () => post("/notifications/read-all"),
};

// ---------- 감사 로그 ----------
export const auditLogsApi = {
  list: (query) => get("/audit-logs", query), // {company_id?, page?, size?}
};
