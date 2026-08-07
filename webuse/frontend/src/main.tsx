import ReactDOM from "react-dom/client";
import {
  createBrowserRouter,
  Navigate,
  RouterProvider,
} from "react-router-dom";
import "./index.css";
import Layout from "./Layout";
import Info from "./views/Info";
import Driver from "./views/Driver";

// StrictMode is intentionally omitted: the Driver opens long-lived EventSource
// and WebSocket connections whose double-mount in dev would spawn duplicates.
const router = createBrowserRouter([
  {
    path: "/",
    element: <Layout />,
    children: [
      { index: true, element: <Navigate to="/info" replace /> },
      { path: "info", element: <Info /> },
      { path: "app/:handle", element: <Driver /> },
    ],
  },
]);

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <RouterProvider router={router} />,
);
