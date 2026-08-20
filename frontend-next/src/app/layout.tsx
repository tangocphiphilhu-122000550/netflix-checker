import type { Metadata, Viewport } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Netflix Checker",
  description: "Netflix cookie checker — user & admin",
  appleWebApp: {
    capable: true,
    statusBarStyle: "black-translucent",
    title: "Netflix Checker",
  },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  maximumScale: 5,
  viewportFit: "cover",
  themeColor: "#0b0b0f",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="vi">
      <body>
        <div className="bg-glow" />
        {children}
      </body>
    </html>
  );
}
