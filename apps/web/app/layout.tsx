import type { Metadata } from "next";
import "./globals.css";
export const metadata: Metadata = { title: "Forge — From idea to deployed app", description: "AI agents build and deploy your web app with you in control." };
export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) { return <html lang="en"><body>{children}</body></html>; }
