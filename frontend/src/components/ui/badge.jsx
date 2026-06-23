import * as React from "react";
import { cva } from "class-variance-authority";
import { cn, SEVERITY_STYLES } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center rounded-md border px-2 py-0.5 text-xs font-medium",
  {
    variants: {
      variant: {
        default: "border-transparent bg-primary text-primary-foreground",
        secondary: "border-transparent bg-secondary text-secondary-foreground",
        outline: "text-foreground",
        muted: "border-transparent bg-muted text-muted-foreground",
      },
    },
    defaultVariants: { variant: "default" },
  }
);

function Badge({ className, variant, ...props }) {
  return <div className={cn(badgeVariants({ variant }), className)} {...props} />;
}

/**
 * Severity-coloured badge — the dashboard's primary visual signal.
 * Falls back to a muted style for unknown severities so we never crash
 * on a future severity level the backend rolls out before the UI ships.
 */
function SeverityBadge({ severity, className, ...props }) {
  const styles = SEVERITY_STYLES[severity] ?? "bg-muted text-muted-foreground border-muted";
  return (
    <div
      className={cn(
        "inline-flex items-center rounded-md border px-2 py-0.5 text-xs font-medium uppercase tracking-wider",
        styles,
        className
      )}
      {...props}
    >
      {severity}
    </div>
  );
}

export { Badge, badgeVariants, SeverityBadge };
