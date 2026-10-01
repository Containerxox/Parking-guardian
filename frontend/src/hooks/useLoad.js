import { useCallback, useEffect, useState } from "react";

// loader(Promise를 돌려주는 함수)를 실행하고 loading / error / data 상태를 관리한다.
// loader는 호출하는 쪽에서 useCallback으로 고정해야 한다. loader가 바뀌면 다시 불러온다.
export default function useLoad(loader) {
  const [state, setState] = useState({ data: null, loading: true, error: "" });
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let alive = true;
    setState((s) => ({ ...s, loading: true, error: "" }));
    loader().then(
      (data) => {
        if (alive) setState({ data, loading: false, error: "" });
      },
      (err) => {
        if (alive) {
          setState({
            data: null,
            loading: false,
            error: (err && err.message) || "데이터를 불러오지 못했습니다.",
          });
        }
      }
    );
    return () => {
      alive = false;
    };
  }, [loader, tick]);

  const reload = useCallback(() => setTick((t) => t + 1), []);

  const setData = useCallback((updater) => {
    setState((s) => ({
      ...s,
      data: typeof updater === "function" ? updater(s.data) : updater,
    }));
  }, []);

  return { data: state.data, loading: state.loading, error: state.error, reload, setData };
}
