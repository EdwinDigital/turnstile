import { Radio as RadioPrimitive } from "@base-ui/react/radio"
import { RadioGroup as RadioGroupPrimitive } from "@base-ui/react/radio-group"

import { cn } from "../../lib/utils"

function RadioGroup({ className, ...props }: RadioGroupPrimitive.Props) {
  return <RadioGroupPrimitive data-slot="radio-group" className={cn(className)} {...props} />
}

function RadioGroupItem({ className, ...props }: RadioPrimitive.Root.Props) {
  return (
    <RadioPrimitive.Root data-slot="radio-group-item" className={cn(className)} {...props}>
      <RadioPrimitive.Indicator data-slot="radio-group-indicator">
        <span />
      </RadioPrimitive.Indicator>
    </RadioPrimitive.Root>
  )
}

export { RadioGroup, RadioGroupItem }