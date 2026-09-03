import { lazy, Suspense } from "react";
import { BrowserRouter, Route, Routes } from "react-router";
import type { AuthApi } from "./api/auth-api";
import { SessionProvider } from "./auth/session";
import { LoadingState, NotFoundState } from "./components/AsyncStates";
import type { AdminApi, AdminView } from "./features/admin";
import {
  AnonymousLayout,
  AuthenticatedLayout,
  RequireCapability,
  UserLayout,
} from "./router/layouts";
import {
  FeedFoundationPage,
  LoginPage,
  RegisterPage,
} from "./router/pages";

const AdminExperience = lazy(async () => {
  const module = await import("./features/admin");
  return { default: module.AdminExperience };
});

function AdminRoutePage({ adminApi, view }: { adminApi?: AdminApi; view: AdminView }) {
  return (
    <div className="workspace-stack">
      <Suspense fallback={<LoadingState label="Loading administration workspace" />}>
        <AdminExperience api={adminApi} view={view} />
      </Suspense>
    </div>
  );
}

export function AppRoutes({ adminApi }: { adminApi?: AdminApi } = {}) {
  return (
    <Routes>
      <Route element={<AnonymousLayout />}>
        <Route path="login" element={<LoginPage />} />
        <Route path="register" element={<RegisterPage />} />
      </Route>
      <Route element={<AuthenticatedLayout />}>
        <Route element={<UserLayout />}>
          <Route index element={<FeedFoundationPage />} />
          <Route element={<RequireCapability capability="dashboardRead" />}>
            <Route
              path="dashboard"
              element={<AdminRoutePage adminApi={adminApi} view="dashboard" />}
            />
            <Route
              path="operations"
              element={<AdminRoutePage adminApi={adminApi} view="operations" />}
            />
          </Route>
          <Route element={<RequireCapability capability="roleManagement" />}>
            <Route
              path="admin/users"
              element={<AdminRoutePage adminApi={adminApi} view="roles" />}
            />
          </Route>
        </Route>
      </Route>
      <Route path="*" element={<NotFoundState />} />
    </Routes>
  );
}

export function App({ adminApi, authApi }: { adminApi?: AdminApi; authApi?: AuthApi }) {
  return (
    <SessionProvider api={authApi}>
      <BrowserRouter>
        <AppRoutes adminApi={adminApi} />
      </BrowserRouter>
    </SessionProvider>
  );
}
