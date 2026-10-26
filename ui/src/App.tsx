import { Navigate, Route, Routes } from "react-router-dom";
import Layout from "./components/Layout";
import PartnersPage from "./pages/PartnersPage";
import PartnerOnboardPage from "./pages/PartnerOnboardPage";
import PartnerDetailPage from "./pages/PartnerDetailPage";
import ApprovalsPage from "./pages/ApprovalsPage";
import VerificationsPage from "./pages/VerificationsPage";
import VerificationDetailPage from "./pages/VerificationDetailPage";
import DecisionsPage from "./pages/DecisionsPage";
import MyConsentsPage from "./pages/MyConsentsPage";
import ConsentRequestPage from "./pages/ConsentRequestPage";
import FarmerLayout from "./components/FarmerLayout";
import { isFarmerMode } from "./auth";

export default function App() {
  // Farmer pages with national-ID login (beneficiaryAuth on): no console.
  if (isFarmerMode()) {
    return (
      <Routes>
        <Route element={<FarmerLayout />}>
          <Route path="/consent/:requestId" element={<ConsentRequestPage />} />
          <Route path="/my/consents" element={<MyConsentsPage />} />
          <Route path="*" element={<Navigate to="/my/consents" replace />} />
        </Route>
      </Routes>
    );
  }
  return (
    <Routes>
      {/* Subject consent-giving screen — standalone, no admin chrome. */}
      <Route path="/consent/:requestId" element={<ConsentRequestPage />} />

      {/* Everything else lives inside the console shell. */}
      <Route element={<Layout />}>
        <Route index element={<Navigate to="/partners" replace />} />
        <Route path="/partners" element={<PartnersPage />} />
        <Route path="/partners/new" element={<PartnerOnboardPage />} />
        <Route path="/partners/:id" element={<PartnerDetailPage />} />
        <Route path="/approvals" element={<ApprovalsPage />} />
        <Route path="/verifications" element={<VerificationsPage />} />
        <Route path="/verifications/:id" element={<VerificationDetailPage />} />
        <Route path="/decisions" element={<DecisionsPage />} />
        <Route path="/my/consents" element={<MyConsentsPage />} />
        <Route path="*" element={<Navigate to="/partners" replace />} />
      </Route>
    </Routes>
  );
}
