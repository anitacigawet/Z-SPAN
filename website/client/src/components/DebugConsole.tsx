import { useState, useEffect } from "react";
import { Terminal, X, ChevronDown, ChevronUp } from "lucide-react";

export interface DebugLog {
 id: string;
 timestamp: string;
 type: "info" | "success" | "error" | "warning";
 category: string;
 message: string;
 details?: any;
}

interface DebugConsoleProps {
 logs: DebugLog[];
 onClear?: () => void;
}

export function DebugConsole({ logs, onClear }: DebugConsoleProps) {
 const [isOpen, setIsOpen] = useState(false);
 const [isMinimized, setIsMinimized] = useState(false);

 // Auto-scroll to bottom when new logs arrive
 useEffect(() => {
 if (isOpen && !isMinimized) {
 const logContainer = document.getElementById("debug-log-container");
 if (logContainer) {
 logContainer.scrollTop = logContainer.scrollHeight;
 }
 }
 }, [logs, isOpen, isMinimized]);

 const getTypeColor = (type: DebugLog["type"]) => {
 switch (type) {
 case "success":
 return "#00ff00";
 case "error":
 return "#ff0055";
 case "warning":
 return "#ffaa00";
 default:
 return "#00ffff";
 }
 };

 const getTypeIcon = (type: DebugLog["type"]) => {
 switch (type) {
 case "success":
 return "✓";
 case "error":
 return "✗";
 case "warning":
 return "⚠";
 default:
 return "ℹ";
 }
 };

 if (!isOpen) {
 return (
 <button
 onClick={() => setIsOpen(true)}
 className="debug-console-toggle"
 title="Open Debug Console"
 >
 <Terminal size={20} />
 {logs.length > 0 && <span className="debug-badge">{logs.length}</span>}
 </button>
 );
 }

 return (
 <div className={`debug-console ${isMinimized ? "minimized" : ""}`}>
 <div className="debug-console-header">
 <div className="debug-console-title">
 <Terminal size={16} />
 <span>Debug Console</span>
 <span className="debug-log-count">({logs.length} logs)</span>
 </div>
 <div className="debug-console-actions">
 {onClear && (
 <button
 onClick={onClear}
 className="debug-action-btn"
 title="Clear logs"
 >
 Clear
 </button>
 )}
 <button
 onClick={() => setIsMinimized(!isMinimized)}
 className="debug-action-btn"
 title={isMinimized ? "Expand" : "Minimize"}
 >
 {isMinimized ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
 </button>
 <button
 onClick={() => setIsOpen(false)}
 className="debug-action-btn"
 title="Close"
 >
 <X size={16} />
 </button>
 </div>
 </div>

 {!isMinimized && (
 <div id="debug-log-container" className="debug-console-logs">
 {logs.length === 0 ? (
 <div className="debug-empty">No debug logs yet</div>
 ) : (
 logs.map(log => (
 <div key={log.id} className="debug-log-entry">
 <div className="debug-log-header">
 <span
 className="debug-log-icon"
 style={{ color: getTypeColor(log.type) }}
 >
 {getTypeIcon(log.type)}
 </span>
 <span className="debug-log-timestamp">{log.timestamp}</span>
 <span className="debug-log-category">[{log.category}]</span>
 </div>
 <div className="debug-log-message">{log.message}</div>
 {log.details && (
 <details className="debug-log-details">
 <summary>Details</summary>
 <pre>{JSON.stringify(log.details, null, 2)}</pre>
 </details>
 )}
 </div>
 ))
 )}
 </div>
 )}
 </div>
 );
}
