import type { Metadata } from "next";
import AppShell from "@/components/AppShell";
import "./globals.css";

export const metadata: Metadata = {
  title: "VAYUNX Crypto Profiler",
  description: "What does switching crypto algorithms cost? Lab benchmarks and app profiling, side by side.",
};

// Applies the saved theme before paint (no flash). "system" = no attribute; the CSS media query decides.
const themeScript = `try{var t=localStorage.getItem("vx-theme");if(t==="light"||t==="dark")document.documentElement.dataset.theme=t}catch(e){}`;

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
      </head>
      <body>
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
