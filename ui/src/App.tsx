import { Navigate, Route, Routes } from "react-router-dom";
import Layout, { homePath } from "./components/Layout";
import VerificationsPage from "./pages/VerificationsPage";
import VerificationDetailPage from "./pages/VerificationDetailPage";
import DecisionsPage from "./pages/DecisionsPage";
import ConsentDetailPage from "./pages/ConsentDetailPage";

// The staff console. Farmer-facing screens (giving and withdrawing consent)
// belong to the self-service portal, not here.
export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Navigate to={homePath()} replace />} />
        <Route path="/verifications" element={<VerificationsPage />} />
        <Route path="/verifications/:id" element={<VerificationDetailPage />} />
        <Route path="/consents/:id" element={<ConsentDetailPage />} />
        <Route path="/decisions" element={<DecisionsPage />} />
        <Route path="*" element={<Navigate to={homePath()} replace />} />
      </Route>
    </Routes>
  );
}
