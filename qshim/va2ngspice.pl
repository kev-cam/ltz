#!/usr/bin/env perl
#
# va2ngspice.pl <xyce_deck> <workdir> <openvaf> [va_search_dir...]
#
# Convert a Xyce-flavour Verilog-A deck to ngspice's OSDI form so the qshim
# bridge can funnel VA-using QSPICE schematics to ngspice. Two transforms:
#   .hdl "mod.va"            -> OpenVAF-compile mod.va to <workdir>/mod.osdi and
#                              emit a `pre_osdi <osdi>` line (for the .control block)
#   Y<MODULE> <inst> <nodes> <params>  ->  N<inst> <nodes> <module> <params>
#     (ngspice instantiates an OSDI device with N<name>; the module name moves
#      from the Y prefix to after the node list; nodes are the bare tokens,
#      params are the `key=val` tokens. Module name is lower-cased to match the
#      Verilog-A `module <name>` -- QSPICE Y-devices are upper-cased by name.)
#
# Writes <workdir>/body.cir (the converted deck, .hdl lines dropped) and
# <workdir>/pre_osdi.txt (the pre_osdi lines). Reuses the bfit drivers_ngspice
# pipeline (OpenVAF -> OSDI -> pre_osdi), generalised to the Xyce Y-device form.
use strict;
use warnings;
use File::Basename qw(dirname basename);

my ($deck, $wk, $openvaf, @search) = @ARGV;
die "usage: $0 <xyce_deck> <workdir> <openvaf> [va_search_dir...]\n"
    unless $deck && $wk && $openvaf;
my $deckdir = dirname($deck);
mkdir $wk unless -d $wk;

sub find_va {
    my ($f) = @_;
    return $f if -f $f;
    for my $d ($deckdir, @search) {
        next unless defined $d && length $d;
        return "$d/$f" if -f "$d/$f";
        my @g = grep { -f } glob("$d/$f");
        return $g[0] if @g;
        chomp(my $r = `find "$d" -name "$f" 2>/dev/null | head -1`);
        return $r if $r && -f $r;
    }
    return undef;
}

open my $in,   '<', $deck            or die "va2ngspice: open $deck: $!\n";
open my $body, '>', "$wk/body.cir"   or die "va2ngspice: $wk/body.cir: $!\n";
open my $pre,  '>', "$wk/pre_osdi.txt" or die "va2ngspice: $wk/pre_osdi.txt: $!\n";
my (%seen, %inst_param);   # inst_param{module}{param}=1 for type="instance" params

while (my $l = <$in>) {
    $l =~ s/\r?\n$//;
    if ($l =~ /^\s*\.(?:hdl|va)\s+(.+?)\s*$/i) {
        (my $f = $1) =~ s/^["']|["']$//g;
        my $path = find_va($f) or die "va2ngspice: cannot find Verilog-A '$f' (searched $deckdir @search)\n";
        my $base = basename($path); $base =~ s/\.[^.]+$//;
        my $osdi = "$wk/$base.osdi";
        unless ($seen{$osdi}++) {
            # OpenVAF rejects Xyce-only module attributes (xyceModelGroup /
            # xyceLevelNumber); strip them into a sanitized copy first, keeping
            # the standard type="instance" params OpenVAF needs.
            my $src = "$wk/$base.va";
            {
                open my $vi, '<', $path or die "va2ngspice: read $path: $!\n";
                local $/; my $t = <$vi>; close $vi;
                $t =~ s/\bxyce\w*\s*=\s*"[^"]*"//g;   # drop xyceModelGroup/xyceLevelNumber/...
                $t =~ s/\(\*\s*\*\)//g;                # remove now-empty attribute blocks
                open my $vo, '>', $src or die "va2ngspice: write $src: $!\n";
                print $vo $t; close $vo;
                # classify params: (* type="instance" *) -> N-line param; the rest
                # are model params (-> the .model line). ngspice rejects a model
                # param given as an instance param and vice-versa.
                my $mn = ($t =~ /module\s+(\w+)/) ? $1 : $base;
                while ($t =~ /\(\*[^*]*type\s*=\s*"instance"[^*]*\*\)\s*parameter\s+\w+\s+(\w+)/gi) {
                    $inst_param{$mn}{lc $1} = 1;
                }
            }
            system($openvaf, $src, '-o', $osdi) == 0
                or die "va2ngspice: OpenVAF failed on $path\n";
            print $pre "pre_osdi $osdi\n";
        }
        next;   # ngspice doesn't understand .hdl -- the osdi is loaded in .control
    }
    if ($l =~ /^Y(\S+)\s+(\S+)\s+(.*)$/i) {
        my ($mod, $inst, $rest) = (lc $1, $2, $3);
        $rest =~ s/\s*[;*].*$//;            # strip trailing comment
        my $ip = $inst_param{$mod} || {};
        my (@nodes, @iparams, @mparams);
        for my $t (split /\s+/, $rest) {
            next unless length $t;
            if ($t =~ /^(\w+)\s*=/) {
                if ($ip->{lc $1}) { push @iparams, $t } else { push @mparams, $t }
            } else { push @nodes, $t }
        }
        # ngspice binds an OSDI device via a per-instance .model (model params) +
        # an N<inst> line (nodes, then model name, then instance params).
        my $mname = "${mod}_${inst}";
        print $body ".model $mname $mod(@mparams)\n";
        print $body "N$inst @nodes $mname @iparams\n";
        next;
    }
    print $body "$l\n";
}
close $in; close $body; close $pre;
