import { Link, useParams } from "react-router-dom";
import { ChevronLeft } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

/** Day 1 stub. Findings table + summary + commit list land in Week 4 Day 3. */
export default function ReviewDetail() {
  const { reviewId } = useParams();
  return (
    <section className="space-y-6">
      <div>
        <Button asChild variant="ghost" size="sm">
          <Link to="/projects">
            <ChevronLeft className="size-4" />
            Back
          </Link>
        </Button>
      </div>
      <Card>
        <CardHeader>
          <CardTitle>Review {reviewId}</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          Summary, severity badges, findings table, and PDF download land in Week 4 Days 3–4.
        </CardContent>
      </Card>
    </section>
  );
}
