import { BrowserRouter, Route, Routes } from "react-router";
import type { AuthApi } from "./api/auth-api";
import { SessionProvider } from "./auth/session";
import { NotFoundState } from "./components/AsyncStates";
import {
  AnonymousLayout,
  AuthenticatedLayout,
  OperationsLayout,
  RequireCapability,
  UserLayout,
} from "./router/layouts";
import {
  DashboardFoundationPage,
  FeedFoundationPage,
  LoginPage,
  OperationsFoundationPage,
  RegisterPage,
  RoleManagementFoundationPage,
} from "./router/pages";

export function AppRoutes() {
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
            <Route path="dashboard" element={<DashboardFoundationPage />} />
          </Route>
          <Route element={<RequireCapability capability="operationsWrite" />}>
            <Route path="operations" element={<OperationsLayout />}>
              <Route index element={<OperationsFoundationPage />} />
            </Route>
          </Route>
          <Route element={<RequireCapability capability="roleManagement" />}>
            <Route path="admin/users" element={<RoleManagementFoundationPage />} />
          </Route>
        </Route>
      </Route>
      <Route path="*" element={<NotFoundState />} />
    </Routes>
  );
}

export function App({ authApi }: { authApi?: AuthApi }) {
  return (
    <SessionProvider api={authApi}>
      <BrowserRouter>
        <AppRoutes />
      </BrowserRouter>
    </SessionProvider>
  );
}
