import { lazy, Suspense } from "react";
import { Route, Routes } from "react-router";
import { InvestigationListPage } from "./pages/InvestigationListPage";
import { LoginPage } from "./pages/LoginPage";

// The detail page carries the flow graph library; the other pages load without it.
const InvestigationDetailPage = lazy(() =>
  import("./pages/InvestigationDetailPage").then((module) => ({ default: module.InvestigationDetailPage })),
);

export function App() {
  return (
    <Routes>
      <Route path="/" element={<InvestigationListPage />} />
      <Route path="/login" element={<LoginPage />} />
      <Route
        path="/investigations/:id"
        element={
          <Suspense>
            <InvestigationDetailPage />
          </Suspense>
        }
      />
    </Routes>
  );
}
