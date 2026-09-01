import React from "react";
import ReactDOM from "react-dom/client";
import "./styles.css";

function FoundationStatus() {
  return (
    <main>
      <h1>MicroLens Recommendation MVP</h1>
      <p>Foundation health shell only.</p>
      <p>Recommendation, Dashboard, authentication, and operations UI are not implemented yet.</p>
    </main>
  );
}

const root = document.getElementById("root");
if (!root) throw new Error("Missing #root element");

ReactDOM.createRoot(root).render(
  <React.StrictMode>
    <FoundationStatus />
  </React.StrictMode>,
);
