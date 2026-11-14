import { Navigate, Route, Routes } from "react-router-dom";
import Layout from "./components/Layout";
import HistoryPage from "./pages/HistoryPage";
import NewRequestPage from "./pages/NewRequestPage";
import RequestDetailPage from "./pages/RequestDetailPage";
import ConsentsPage from "./pages/ConsentsPage";
import ConsentDetailPage from "./pages/ConsentDetailPage";
import TemplatesPage from "./pages/TemplatesPage";

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Navigate to="/consents" replace />} />
        <Route path="/consents" element={<ConsentsPage />} />
        <Route path="/consents/new" element={<NewRequestPage />} />
        <Route path="/consents/:id" element={<ConsentDetailPage />} />
        <Route path="/requests/:id" element={<RequestDetailPage />} />
        <Route path="/templates" element={<TemplatesPage />} />
        <Route path="/history" element={<HistoryPage />} />
        {/* Earlier paths */}
        <Route path="/requests" element={<Navigate to="/history" replace />} />
        <Route path="/requests/new" element={<Navigate to="/consents/new" replace />} />
        <Route path="/access" element={<Navigate to="/templates" replace />} />
        <Route path="/use-cases" element={<Navigate to="/templates" replace />} />
        <Route path="*" element={<Navigate to="/consents" replace />} />
      </Route>
    </Routes>
  );
}
