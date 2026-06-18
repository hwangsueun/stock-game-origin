import type { ReactNode } from "react";

type PixelPanelProps = {
  children: ReactNode;
  title?: string;
};

export function PixelPanel({ children, title }: PixelPanelProps) {
  return (
    <section style={panelStyle}>
      {title && <h2 style={titleStyle}>{title}</h2>}
      {children}
    </section>
  );
}

const panelStyle: React.CSSProperties = {
  border: "3px solid #f7e72f",
  background: "#242424",
  boxShadow: "6px 6px 0 #000",
  padding: "18px",
  marginBottom: "18px",
};

const titleStyle: React.CSSProperties = {
  marginTop: 0,
  marginBottom: "14px",
  color: "#f7e72f",
};