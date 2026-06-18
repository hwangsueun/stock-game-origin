export enum RepaymentGrade {
  EXCELLENT = "EXCELLENT", // 전액 상환
  GOOD = "GOOD",           // 80% 이상
  NORMAL = "NORMAL",       // 50% 이상
  BAD = "BAD",             // 1% 이상 (일부)
  DANGER = "DANGER",       // 0% (상환 불가)
}