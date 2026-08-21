import { createRoot } from "react-dom/client";
import App from "./App";
import "./index.css";
import ErrorBoundary from "./components/ErrorBoundary";
import MaintenancePage from "./MaintenancePage";

const maintenanceMode =
  import.meta.env.VITE_SITE_MODE?.trim().toLowerCase() === "maintenance";

if (maintenanceMode) {
  document.title = "Z-SPAN — Under maintenance";
}

createRoot(document.getElementById("root")!).render(
  maintenanceMode ? (
    <MaintenancePage />
  ) : (
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  )
);
