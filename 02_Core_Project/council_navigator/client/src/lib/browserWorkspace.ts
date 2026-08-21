export interface BrowserWorkspaceEntry {
  id: string;
  userId: number;
  meetingId: number;
  query: string;
  answer: string;
  provider: string;
  model: string;
  inputTokens: number;
  outputTokens: number;
  costUsd: number;
  runId: string;
  createdAt: string;
}

const DB_NAME = "zspan-member-workspace-v1";
const STORE_NAME = "analyses";
const DB_VERSION = 1;

function openWorkspace(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    if (typeof indexedDB === "undefined") {
      reject(new Error("Local browser storage is unavailable."));
      return;
    }
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onerror = () => reject(request.error ?? new Error("Could not open local workspace."));
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(STORE_NAME)) {
        const store = db.createObjectStore(STORE_NAME, { keyPath: "id" });
        store.createIndex("userId", "userId", { unique: false });
      }
    };
    request.onsuccess = () => resolve(request.result);
  });
}

function complete(transaction: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => reject(transaction.error ?? new Error("Local workspace write failed."));
    transaction.onabort = () => reject(transaction.error ?? new Error("Local workspace write was cancelled."));
  });
}

export async function saveBrowserWorkspaceEntry(
  entry: BrowserWorkspaceEntry,
): Promise<void> {
  const db = await openWorkspace();
  try {
    const transaction = db.transaction(STORE_NAME, "readwrite");
    transaction.objectStore(STORE_NAME).put(entry);
    await complete(transaction);
  } finally {
    db.close();
  }
}

export async function listBrowserWorkspaceEntries(
  userId: number,
): Promise<BrowserWorkspaceEntry[]> {
  const db = await openWorkspace();
  try {
    const transaction = db.transaction(STORE_NAME, "readonly");
    const request = transaction.objectStore(STORE_NAME).index("userId").getAll(userId);
    const rows = await new Promise<BrowserWorkspaceEntry[]>((resolve, reject) => {
      request.onsuccess = () => resolve(request.result as BrowserWorkspaceEntry[]);
      request.onerror = () => reject(request.error ?? new Error("Could not read local workspace."));
    });
    return rows.sort((left, right) => right.createdAt.localeCompare(left.createdAt));
  } finally {
    db.close();
  }
}

export async function clearBrowserWorkspaceEntries(userId: number): Promise<void> {
  const entries = await listBrowserWorkspaceEntries(userId);
  if (!entries.length) return;
  const db = await openWorkspace();
  try {
    const transaction = db.transaction(STORE_NAME, "readwrite");
    const store = transaction.objectStore(STORE_NAME);
    for (const entry of entries) store.delete(entry.id);
    await complete(transaction);
  } finally {
    db.close();
  }
}
