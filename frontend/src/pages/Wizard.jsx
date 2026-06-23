import { Link } from "react-router-dom";
import { ChevronLeft } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

/** Day 1 stub. 4-screen wizard (paste URL → probe branches → pick + checklist
 *  → indexing progress) lands in Week 4 Day 2. */
export default function Wizard() {
  return (
    <section className="space-y-6">
      <div>
        <Button asChild variant="ghost" size="sm">
          <Link to="/projects">
            <ChevronLeft className="size-4" />
            Cancel
          </Link>
        </Button>
      </div>
      <Card>
        <CardHeader>
          <CardTitle>New project</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          The 4-screen onboarding wizard lands tomorrow (Week 4 Day 2):
          paste URL → discover branches → pick branches + checklist → live indexing progress.
        </CardContent>
      </Card>
    </section>
  );
}
