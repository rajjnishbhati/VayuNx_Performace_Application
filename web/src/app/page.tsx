import { Suspense } from "react";
import ComparePage from "@/components/ComparePage";
import { Skeleton } from "@/components/States";

export default function Home() {
  return (
    <Suspense fallback={<Skeleton />}>
      <ComparePage />
    </Suspense>
  );
}
