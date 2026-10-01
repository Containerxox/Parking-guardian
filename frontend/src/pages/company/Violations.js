import React from "react";
import Page from "../../components/ui/Page";
import ViolationBoard from "../../components/violations/ViolationBoard";

// COMPANY_ADMIN 의 첫 화면
export default function Violations() {
  return (
    <Page title="장애인 주차 위반 내역" subtitle="위반을 확인하고 현장 대응 결과를 기록합니다.">
      <ViolationBoard scope="company" />
    </Page>
  );
}
