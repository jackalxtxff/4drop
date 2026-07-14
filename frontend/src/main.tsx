import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import "./index.css";
import { Layout } from "./components/Layout";
import { initTheme } from "./components/ThemeToggle";
import { auth } from "./api";

initTheme();
import { ConnectionsPage } from "./pages/Connections";
import { LoginPage } from "./pages/Login";
import { ProductsPage } from "./pages/Products";

function Protected({ children }: { children: React.ReactNode }) {
  if (!auth.token) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route
          element={
            <Protected>
              <Layout />
            </Protected>
          }
        >
          <Route path="/products" element={<ProductsPage />} />
          <Route path="/connections" element={<ConnectionsPage />} />
        </Route>
        <Route path="*" element={<Navigate to="/products" replace />} />
      </Routes>
    </BrowserRouter>
  </React.StrictMode>,
);
