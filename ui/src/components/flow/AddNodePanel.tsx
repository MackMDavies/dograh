import * as LucideIcons from 'lucide-react';
import { ArrowRight, Circle, ExternalLink, type LucideIcon, X } from 'lucide-react';
import { useEffect, useMemo } from 'react';

import type { NodeSpec } from '@/client/types.gen';
import { useNodeSpecs } from '@/components/flow/renderer';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

import { NodeType } from './types';

type AddNodePanelProps = {
    isOpen: boolean;
    onClose: () => void;
    onNodeSelect: (nodeType: NodeType) => void;
};

const SECTION_ORDER: Array<{ category: NodeSpec['category']; title: string; dotColor: string }> = [
    { category: 'trigger',     title: 'Triggers',     dotColor: 'bg-purple-500' },
    { category: 'call_node',   title: 'Agent Nodes',  dotColor: 'bg-blue-500'   },
    { category: 'global_node', title: 'Global Nodes', dotColor: 'bg-amber-500'  },
    { category: 'integration', title: 'Integrations', dotColor: 'bg-teal-500'   },
];

// Matches the badge/color palette in GenericNode exactly
const NODE_STYLE: Record<string, { iconColor: string; bgColor: string; borderColor: string }> = {
    startCall:  { iconColor: 'text-emerald-400', bgColor: 'bg-emerald-500/10', borderColor: 'border-emerald-500/30' },
    agentNode:  { iconColor: 'text-blue-400',    bgColor: 'bg-blue-500/10',    borderColor: 'border-blue-500/30'    },
    endCall:    { iconColor: 'text-rose-400',    bgColor: 'bg-rose-500/10',    borderColor: 'border-rose-500/30'    },
    globalNode: { iconColor: 'text-amber-400',   bgColor: 'bg-amber-500/10',   borderColor: 'border-amber-500/30'  },
    trigger:    { iconColor: 'text-purple-400',  bgColor: 'bg-purple-500/10',  borderColor: 'border-purple-500/30' },
    webhook:    { iconColor: 'text-indigo-400',  bgColor: 'bg-indigo-500/10',  borderColor: 'border-indigo-500/30' },
    qa:         { iconColor: 'text-teal-400',    bgColor: 'bg-teal-500/10',    borderColor: 'border-teal-500/30'   },
    tuner:      { iconColor: 'text-cyan-400',    bgColor: 'bg-cyan-500/10',    borderColor: 'border-cyan-500/30'   },
};

const FALLBACK_STYLE = { iconColor: 'text-zinc-400', bgColor: 'bg-zinc-500/10', borderColor: 'border-zinc-500/30' };

function resolveIcon(name: string): LucideIcon {
    const icons = LucideIcons as unknown as Record<string, LucideIcon>;
    return icons[name] ?? Circle;
}

function NodeCard({
    spec,
    onNodeSelect,
}: {
    spec: NodeSpec;
    onNodeSelect: (nodeType: NodeType) => void;
}) {
    const Icon = resolveIcon(spec.icon);
    const style = NODE_STYLE[spec.name] ?? FALLBACK_STYLE;

    return (
        <button
            type="button"
            onClick={() => onNodeSelect(spec.name as NodeType)}
            className={cn(
                'group w-full flex items-center gap-3 rounded-lg border p-3',
                'bg-card/50 hover:bg-card',
                'border-border/50 hover:border-border',
                'transition-all duration-150 cursor-pointer text-left',
                'hover:shadow-sm',
            )}
        >
            {/* Icon badge */}
            <div
                className={cn(
                    'flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border',
                    style.bgColor,
                    style.borderColor,
                )}
            >
                <Icon className={cn('h-5 w-5', style.iconColor)} />
            </div>

            {/* Text */}
            <div className="min-w-0 flex-1">
                <p className="text-sm font-semibold leading-tight text-foreground">
                    {spec.display_name}
                </p>
                <p className="mt-0.5 text-xs leading-snug text-muted-foreground line-clamp-2">
                    {spec.description}
                </p>
            </div>

            {/* Arrow hint */}
            <ArrowRight
                className={cn(
                    'h-4 w-4 shrink-0 text-muted-foreground/40',
                    'transition-all duration-150',
                    'group-hover:text-muted-foreground group-hover:translate-x-0.5',
                )}
            />
        </button>
    );
}

function NodeSection({
    title,
    dotColor,
    specs,
    onNodeSelect,
}: {
    title: string;
    dotColor: string;
    specs: NodeSpec[];
    onNodeSelect: (nodeType: NodeType) => void;
}) {
    if (specs.length === 0) return null;
    return (
        <div className="space-y-2">
            <div className="flex items-center gap-2 px-0.5">
                <span className={cn('h-1.5 w-1.5 rounded-full shrink-0', dotColor)} />
                <h3 className="text-[11px] font-semibold uppercase tracking-widest text-muted-foreground/70">
                    {title}
                </h3>
            </div>
            <div className="space-y-1.5">
                {specs.map((spec) => (
                    <NodeCard key={spec.name} spec={spec} onNodeSelect={onNodeSelect} />
                ))}
            </div>
        </div>
    );
}

export default function AddNodePanel({ isOpen, onNodeSelect, onClose }: AddNodePanelProps) {
    const { specs } = useNodeSpecs();

    const sections = useMemo(() => {
        return SECTION_ORDER.map(({ category, title, dotColor }) => ({
            title,
            dotColor,
            specs: specs.filter((s) => s.category === category),
        }));
    }, [specs]);

    useEffect(() => {
        const handleKeyDown = (event: KeyboardEvent) => {
            if (event.key === 'Escape' && isOpen) onClose();
        };
        document.addEventListener('keydown', handleKeyDown);
        return () => document.removeEventListener('keydown', handleKeyDown);
    }, [isOpen, onClose]);

    return (
        <div
            className={cn(
                'fixed z-51 right-0 top-0 h-full w-[400px]',
                'bg-background/95 backdrop-blur-sm',
                'border-l border-border/60 shadow-2xl',
                'transform transition-transform duration-300 ease-in-out',
                isOpen ? 'translate-x-0' : 'translate-x-full',
            )}
        >
            {/* Header */}
            <div className="flex items-start justify-between border-b border-border/50 px-5 py-4">
                <div className="space-y-0.5">
                    <h2 className="text-base font-semibold tracking-tight">Add Node</h2>
                    <p className="text-xs text-muted-foreground">
                        Choose a node type to add to your workflow.
                    </p>
                    <a
                        href="https://docs.dograh.com/voice-agent/introduction"
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-1 pt-0.5 text-xs text-muted-foreground/60 hover:text-primary transition-colors"
                    >
                        <ExternalLink className="h-3 w-3" />
                        View documentation
                    </a>
                </div>
                <Button variant="ghost" size="icon" onClick={onClose} className="h-8 w-8 shrink-0 mt-0.5">
                    <X className="h-4 w-4" />
                </Button>
            </div>

            {/* Scrollable node list */}
            <div className="h-[calc(100%-73px)] overflow-y-auto px-4 py-5">
                <div className="space-y-6">
                    {sections.map(({ title, dotColor, specs: sectionSpecs }) => (
                        <NodeSection
                            key={title}
                            title={title}
                            dotColor={dotColor}
                            specs={sectionSpecs}
                            onNodeSelect={onNodeSelect}
                        />
                    ))}
                </div>
            </div>
        </div>
    );
}
