import React, { useEffect, useState } from "react";
import { violationsApi } from "../../api/client";

// 이미지를 요청하지 않고 안내 문구만 보여 줄 경우 그 문구를 돌려준다.
function placeholderText(violation) {
  if (violation.image_status === "EXPIRED") return "보관 기간 만료";
  if (violation.image_status === "FAILED") return "이미지 저장 실패";
  if (!violation.has_image) {
    return violation.image_status === "PENDING" ? "이미지 준비 중" : "이미지 없음";
  }
  return "";
}

// /violations/{id}/image-url 이 돌려준 짧게 유효한 주소를 <img src> 로 쓴다.
export default function ViolationImage({ violation, large = false }) {
  const placeholder = placeholderText(violation);
  const violationId = violation.id;
  const [state, setState] = useState({ url: "", message: "", loading: !placeholder });

  useEffect(() => {
    if (placeholder) return undefined;
    let alive = true;
    setState({ url: "", message: "", loading: true });
    violationsApi.imageUrl(violationId).then(
      (data) => {
        if (alive) setState({ url: data.url, message: "", loading: false });
      },
      (err) => {
        if (!alive) return;
        setState({
          url: "",
          message: err.status === 404 ? "이미지 없음" : "이미지를 불러오지 못했습니다.",
          loading: false,
        });
      }
    );
    return () => {
      alive = false;
    };
  }, [violationId, placeholder]);

  const className = `vio-image ${large ? "is-large" : "is-thumb"}`;
  const text = placeholder || state.message || (state.loading ? "이미지 불러오는 중..." : "");

  if (text || !state.url) {
    return (
      <div className={`${className} is-placeholder`}>
        <span>{text || "이미지 없음"}</span>
      </div>
    );
  }

  return (
    <div className={className}>
      <img
        src={state.url}
        alt="위반 차량 이미지"
        onError={() =>
          setState({ url: "", message: "이미지를 불러오지 못했습니다.", loading: false })
        }
      />
    </div>
  );
}
