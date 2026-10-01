import React from "react";
import Page from "../../components/ui/Page";
import ViolationBoard from "../../components/violations/ViolationBoard";

export default function SuperViolations() {
  return (
    <Page title="전체 위반 내역" subtitle="모든 회사의 위반을 조회하고, 처리 완료된 건의 상태를 복구할 수 있습니다.">
      <ViolationBoard scope="super" />
    </Page>
  );
}
