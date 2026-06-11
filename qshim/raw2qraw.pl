#!/usr/bin/env perl
#
# raw2qraw.pl <xyce.raw> <orig_deck> [--ascii] — re-spell a Xyce ASCII
# rawfile as a QSPICE .qraw so QUX's waveform viewer opens it. Emits BINARY
# by default: in -pipe (GUI) mode QUX refuses ASCII rawfiles ("ASCII files
# are not supported for marching waveforms"). The binary payload, decoded
# from QSPICE64 -binary output, is row-major little-endian doubles with no
# point index. Variable names use QSPICE spellings: V(node)/I(src).
#
use strict;
use warnings;
use POSIX qw(strftime);

my $ascii = grep { $_ eq '--ascii' } @ARGV;
my ($xraw, $deck) = grep { $_ ne '--ascii' } @ARGV;
die "usage: $0 xyce.raw orig_deck [--ascii]\n" unless $xraw && $deck;

open my $fh, '<', $xraw or die "cannot read $xraw\n";
my (@names, @types, $nv, $np, $plot, $cplx);
my ($in_vars, $in_vals, @toks) = (0, 0);
while (my $l = <$fh>) {
    $l =~ s/\r?\n$//;
    if ($l =~ /^Plotname:\s*(.*)/i)        { $plot //= $1; next; }
    if ($l =~ /^Flags:[^\n]*complex/i)     { $cplx = 1; next; }
    if ($l =~ /^No\.\s*Variables:\s*(\d+)/i) { $nv = $1; next; }
    if ($l =~ /^Variables:/i)              { $in_vars = 1; next; }
    if ($l =~ /^Values:/i)                 { $in_vars = 0; $in_vals = 1; next; }
    if ($in_vars && $l =~ /^\s*\d+\s+(\S+)\s+(\S+)/) { push @names, $1; push @types, $2; next; }
    if ($in_vals) {
        push @toks, $l =~ /(-?(?:\d+\.?\d*|\.\d+)(?:e[-+]?\d+)?(?:,-?(?:\d+\.?\d*|\.\d+)(?:e[-+]?\d+)?)?)/ig;
    }
}
close $fh;
die "unparsable Xyce raw\n" unless $nv && @names == $nv;

my @rows;
while (@toks >= $nv + 1) {
    my @grp = splice @toks, 0, $nv + 1;
    shift @grp;
    push @rows, \@grp;
}
$np = scalar @rows;
die "no data points\n" unless $np;

# QSPICE spellings: nodes -> V(node); branch currents -> I(src); keep
# time/frequency/sweep abscissa names.
my @qnames;
for my $i (0 .. $#names) {
    my ($n, $t) = ($names[$i], lc $types[$i]);
    if ($n =~ /^(?:time|frequency|sweep)$/i) { push @qnames, lc $n; next; }
    if ($n =~ /^(\w+)#branch$/i)             { push @qnames, 'I(' . uc($1) . ')'; next; }
    if ($t eq 'voltage')                     { push @qnames, 'V(' . lc($n) . ')'; next; }
    push @qnames, $n;
}

my $title = $deck;
print "Title: * $title (Xyce via qspice-shim)\n";
print 'Date: ' . strftime('%a %b %e %H:%M:%S %Y', localtime) . "\n";
print 'Plotname: ' . ($plot // 'Transient Analysis') . "\n";
print 'Flags: ' . ($cplx ? 'complex' : 'real') . "\n";
printf "Abscissa: %24.15e %24.15e\n", $rows[0][0], $rows[-1][0] if !$cplx;
print "No. Variables: $nv\n";
print "No. Points: $np\n";
print "Command: Xyce (qspice-shim bridge)\n";
print "Variables:\n";
for my $i (0 .. $#qnames) {
    my $t = $i == 0 ? ($qnames[0] eq 'frequency' ? 'frequency' : 'time') : lc $types[$i];
    print "\t$i\t$qnames[$i]\t$t\n";
}
if ($ascii) {
    print "Values:\n";
    for my $p (0 .. $np - 1) {
        print $p;
        for my $v (0 .. $nv - 1) {
            my $val = $rows[$p][$v];
            $val = sprintf('%.15e', $val) unless $val =~ /,/;
            print "\t\t$val\n" if $v == 0;
            print "\t$val\n"  if $v > 0;
        }
    }
} else {
    print "Binary:\n";
    binmode STDOUT;
    for my $p (0 .. $np - 1) {
        for my $v (0 .. $nv - 1) {
            my $val = $rows[$p][$v];
            $val = 0 if $val =~ /,/;   # complex handled upstream as magnitude
            print pack('d<', $val);
        }
    }
}
