import { Navigate, Route, Routes } from "react-router-dom";
import Layout from "./components/Layout";
import { compositeUrl } from "./api/client";
import RequestsPage from "./pages/RequestsPage";
import NewRequestPage from "./pages/NewRequestPage";
import RequestDetailPage from "./pages/RequestDetailPage";
import ConsentsPage from "./pages/ConsentsPage";
import ConsentDetailPage from "./pages/ConsentDetailPage";
import BindingsPage from "./pages/BindingsPage";
import UseCasesPage from "./pages/UseCasesPage";

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Navigate to="/requests" replace />} />
        <Route path="/requests" element={<RequestsPage />} />
        <Route path="/requests/new" element={<NewRequestPage />} />
        <Route path="/requests/:id" element={<RequestDetailPage />} />
        <Route path="/consents" element={<ConsentsPage />} />
        <Route path="/consents/:id" element={<ConsentDetailPage />} />
        <Route path="/access" element={<BindingsPage />} />
        {compositeUrl() && <Route path="/use-cases" element={<UseCasesPage />} />}
        <Route path="*" element={<Navigate to="/requests" replace />} />
      </Route>
    </Routes>
  );
}
