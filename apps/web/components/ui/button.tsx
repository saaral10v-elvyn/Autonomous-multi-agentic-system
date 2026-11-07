import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import type { ButtonHTMLAttributes } from "react";
import { cn } from "@/lib/utils";

const variants = cva("inline-flex items-center justify-center rounded-md text-sm font-medium focus-visible:outline-none disabled:opacity-50", { variants: { variant: { default: "bg-primary text-primary-foreground", outline: "border border-slate-700" }, size: { default: "h-10 px-4", sm: "h-9 px-3", lg: "h-11 px-8" } }, defaultVariants: { variant: "default", size: "default" } });
type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & VariantProps<typeof variants> & { asChild?: boolean };
export function Button({ className, variant, size, asChild = false, ...props }: ButtonProps) { const Comp = asChild ? Slot : "button"; return <Comp className={cn(variants({ variant, size, className }))} {...props} />; }
